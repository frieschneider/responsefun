import numpy as np
import string
from scipy.constants import physical_constants

from sympy.physics.quantum.state import Bra, Ket
from sympy import Symbol, Mul, Add, Pow, symbols, adjoint, im, Float, Integer, S, zoo, I
from itertools import permutations, product, combinations_with_replacement

from responsefun.symbols_and_labels import *
from responsefun.response_operators import MTM, S2S_MTM, ResponseVector, DipoleOperator, DipoleMoment, TransitionFrequency, LeviCivita
from responsefun.sum_over_states import TransitionMoment, SumOverStates
from responsefun.isr_conversion import to_isr, compute_extra_terms
from responsefun.build_tree import build_tree
from responsefun.testdata.cache import MockExcitedStates
from responsefun.bmatrix_vector_product import bmatrix_vector_product
from responsefun.magnetic_dipole_moments import modified_magnetic_transition_moments, gs_magnetic_dipole_moment
from responsefun.adcc_properties import AdccProperties

from adcc import AmplitudeVector
from adcc.workflow import construct_adcmatrix
from adcc.adc_pp import modified_transition_moments
from adcc.adc_pp.state2state_transition_dm import state2state_transition_dm
from adcc.OneParticleOperator import product_trace
from respondo.misc import select_property_method
from respondo.solve_response import solve_response, transition_polarizability, transition_polarizability_complex
from respondo.cpp_algebra import ResponseVector as RV
from tqdm import tqdm


Hartree = physical_constants["hartree-electron volt relationship"][0]
ABC = list(string.ascii_uppercase)

# Levi-Civita tensor
lc_tensor = np.zeros((3, 3, 3))
lc_tensor[0, 1, 2] = lc_tensor[1, 2, 0] = lc_tensor[2, 0, 1] = 1
lc_tensor[2, 1, 0] = lc_tensor[0, 2, 1] = lc_tensor[1, 0, 2] = -1


def _check_omegas_and_final_state(sos_expr, omegas, correlation_btw_freq, gamma_val, final_state):
    """Checks for errors in the entered frequencies or the final state.
    """
    if isinstance(sos_expr, Add):
        arg_list = [a for term in sos_expr.args for a in term.args]
        denom_list = [a.args[0] for a in arg_list if isinstance(a, Pow)]
    else:
        arg_list = [a for a in sos_expr.args]
        denom_list = [a.args[0] for a in arg_list if isinstance(a, Pow)]

    if omegas:
        omega_symbols = [tup[0] for tup in omegas]
        for o in omega_symbols:
            if omega_symbols.count(o) > 1:
                pass
                #raise ValueError("Two different values were given for the same frequency.")

        sum_freq = [freq for tup in correlation_btw_freq for freq in tup[1].args]
        check_dict = {o[0]: False for o in omegas}
        for o in check_dict:
            for denom in denom_list:
                if o in denom.args or -o in denom.args or o in sum_freq or -o in sum_freq:
                    check_dict[o] = True
                    break
        if False in check_dict.values():
            pass
            #raise ValueError(
            #        "A frequency was specified that is not included in the entered SOS expression.\nomegas: {}".format(check_dict)
            #)

    if gamma_val:
        for denom in denom_list:
            if 1.0*gamma*I not in denom.args and -1.0*gamma*I not in denom.args:
                raise ValueError("Although the entered SOS expression is real, a value for gamma was specified.")

    if final_state:
        check_f = False
        for a in arg_list:
            if a == Bra(final_state[0]) or a == Ket(final_state[0]):
                check_f = True
                break
        if check_f == False:
            raise ValueError("A final state was mistakenly specified.")


def find_remaining_indices(sos_expr, summation_indices):
    """Find indices of summation of the entered SOS term and return them in a list. 
    """
    assert isinstance(sos_expr, Mul)
    sum_ind = []
    for a in sos_expr.args:
        if isinstance(a, Bra) or isinstance(a, Ket):
            if a.label[0] in summation_indices and a.label[0] not in sum_ind:
                sum_ind.append(a.label[0])
    return sum_ind


def replace_bra_op_ket(expr):
    """Replace Bra(to_state)*op*Ket(from_state) sequence in a SymPy term
    by an instance of <class 'responsetree.response_operators.DipoleMoment'>.
    """
    assert type(expr) == Mul
    subs_dict = {}
    for ia, a in enumerate(expr.args):
        if isinstance(a, DipoleOperator):
            from_state = expr.args[ia+1]
            to_state = expr.args[ia-1]
            key = to_state*a*from_state
            subs_dict[key] = DipoleMoment(a.comp, str(from_state.label[0]), str(to_state.label[0]), a.op_type)
    return expr.subs(subs_dict)


def from_vec_to_vec(from_vec, to_vec):
    """Evaluate the scalar product between two instances of ResponseVector and/or AmplitudeVector."""
    if isinstance(from_vec, AmplitudeVector):
        fv = RV(from_vec)
    else:
        fv = from_vec.copy()
    if isinstance(to_vec, AmplitudeVector):
        tv = RV(to_vec)
    else:
        tv = to_vec.copy()
    assert isinstance(fv, RV) and isinstance(tv, RV)
    real = fv.real @ tv.real - fv.imag @ tv.imag
    imag = fv.real @ tv.imag + fv.imag @ tv.real
    if imag == 0:
        return real
    else:
        return real + 1j*imag


def evaluate_property_isr(
        state, sos_expr, summation_indices, omegas=None, gamma_val=0.0,
        final_state=None, perm_pairs=None, extra_terms=True, symmetric=False, excluded_cases=None, **solver_args
    ):
    """Compute a molecular property with the ADC/ISR approach from its SOS expression.

    Parameters
    ----------
    state: <class 'adcc.ExcitedStates.ExcitedStates'>
        ExcitedStates object returned by an ADC calculation.

    sos_expr: <class 'sympy.core.add.Add'> or <class 'sympy.core.mul.Mul'>
        SymPy expression of the SOS;
        it can be either the full expression or a single term from which the full expression can be generated via permutation.

    summation_indices: list of <class 'sympy.core.symbol.Symbol'>
        List of indices of summation.

    omegas: list of tuples, optional
        List of (symbol, value) pairs for the frequencies;
        (symbol, value): (<class 'sympy.core.symbol.Symbol'>, <class 'sympy.core.add.Add'> or <class 'sympy.core.symbol.Symbol'> or float),
        e.g., [(w_o, w_1+w_2), (w_1, 0.5), (w_2, 0.5)].

    gamma_val: float, optional

    final_state: tuple, optional
        (<class 'sympy.core.symbol.Symbol'>, int), e.g., (f, 0).

    perm_pairs: list of tuples, optional
        List of (op, freq) pairs whose permutation yields the full SOS expression;
        (op, freq): (<class 'responsetree.response_operators.DipoleOperator'>, <class 'sympy.core.symbol.Symbol'>),
        e.g., [(op_a, -w_o), (op_b, w_1), (op_c, w_2)].

    extra_terms: bool, optional
        Compute the additional terms that arise when converting the SOS expression to its ADC/ISR formulation;
        by default 'True'.

    symmetric: bool, optional
        Resulting tensor is symmetric; 
        by default 'False'.

    Returns
    ----------
    <class 'numpy.ndarray'>
        Resulting tensor.
    """
    matrix = construct_adcmatrix(state.matrix)
    property_method = select_property_method(matrix)
    mp = matrix.ground_state

    if omegas is None:
        omegas = []
    elif type(omegas) == tuple:
        omegas = [omegas]
    else:
        assert type(omegas) == list
    assert type(symmetric) == bool
    
    correlation_btw_freq = [tup for tup in omegas if type(tup[1]) == Symbol or type(tup[1]) == Add]
    all_omegas = omegas.copy()
    if final_state:
        assert type(final_state) == tuple and len(final_state) == 2
        all_omegas.append(
                (TransitionFrequency(str(final_state[0]), real=True),
                state.excitation_energy_uncorrected[final_state[1]])
        )
    else:
        assert final_state is None
    sos = SumOverStates(
            sos_expr, summation_indices, correlation_btw_freq=correlation_btw_freq, perm_pairs=perm_pairs, excluded_cases=excluded_cases
    )
    adcc_prop_dict = {}
    for op_type in sos.operator_types:
        adcc_prop_dict[op_type] = AdccProperties(state, op_type)
    
    _check_omegas_and_final_state(sos.expr, omegas, correlation_btw_freq, gamma_val, final_state)
    isr = to_isr(sos, extra_terms)
    mod_isr = isr.subs(correlation_btw_freq)
    rvecs_dict_list = build_tree(mod_isr)
    
    response_dict = {}
    for tup in rvecs_dict_list:
        root_expr, rvecs_dict = tup
        # check if response equations become equal after inserting values for omegas and gamma
        rvecs_dict_mod = {}
        for k, v in rvecs_dict.items():
            om = float(k[2].subs(all_omegas))
            gam = float(im(k[3].subs(gamma, gamma_val)))
            if gam == 0 and gamma_val != 0:
                raise ValueError(
                        "Although the entered SOS expression is real, a value for gamma was specified."
                )
            new_key = (*k[:2], om, gam, *k[4:])
            if new_key not in rvecs_dict_mod.keys():
                rvecs_dict_mod[new_key] = [v]
            else:
                rvecs_dict_mod[new_key].append(v)
        
        # solve response equations
        for k, v in rvecs_dict_mod.items():
            op_type = k[1]
            if k[0] == MTM:
                rhss = np.array(adcc_prop_dict[op_type].mtms)
                op_dim = adcc_prop_dict[op_type].op_dim
                response_shape = (3,)*op_dim
                iterables = [list(range(shape)) for shape in response_shape]
                components = list(product(*iterables))
                response = np.empty(response_shape, dtype=object)
                if k[3] == 0.0:
                    for c in components:
                        response[c] = solve_response(matrix, rhss[c], -k[2], gamma=0.0, **solver_args)
                else:
                    for c in components:
                        response[c] = solve_response(matrix, RV(rhss[c]), -k[2], gamma=-k[3], **solver_args)
                for vv in v:
                    response_dict[vv] = response
            elif k[0] == S2S_MTM:
                dips = np.array(adcc_prop_dict[op_type].dips)
                op_dim = adcc_prop_dict[op_type].op_dim
                if k[4] == ResponseVector:
                    no = k[5]
                    rvecs = response_dict[no]
                    if k[3] == 0.0:
                        product_vecs_shape = (3,)*op_dim + rvecs.shape
                        iterables = [list(range(shape)) for shape in product_vecs_shape]
                        components = list(product(*iterables))
                        response = np.empty(product_vecs_shape, dtype=object)
                        for c in components:
                            rhs = bmatrix_vector_product(property_method, mp, dips[c[:op_dim]], rvecs[c[op_dim:]])
                            response[c] = solve_response(matrix, rhs, -k[2], gamma=-k[3], **solver_args)
                    else:
                        # complex bmatrix vector product is implemented (but not tested),
                        # but solving response equations with complex right-hand sides is not yet possible
                        raise NotImplementedError("The case of complex response vectors (leading to complex right-hand sides"
                                                  "when solving the response equations) has not yet been implemented.")
                    for vv in v:
                        response_dict[vv] = response
                elif k[4] == final_state[0]:
                    product_vecs_shape = (3,)*op_dim
                    iterables = [list(range(shape)) for shape in product_vecs_shape]
                    components = list(product(*iterables))
                    response = np.empty(product_vecs_shape, dtype=object)
                    if k[3] == 0.0:
                        for c in components:
                            product_vec = bmatrix_vector_product(property_method, mp, dips[c], state.excitation_vector[final_state[1]])
                            response[c] = solve_response(matrix, product_vec, -k[2], gamma=0.0, **solver_args)
                    else:
                        for c in components:
                            product_vec = bmatrix_vector_product(property_method, mp, dips[c], state.excitation_vector[final_state[1]])
                            response[c] = solve_response(matrix, RV(product_vec), -k[2], gamma=-k[3], **solver_args)
                    for vv in v:
                        response_dict[vv] = response
                else:
                    raise ValueError("Unkown response equation.")

            else:
                raise ValueError("Unkown response equation.")
    
    if rvecs_dict_list:
        root_expr = rvecs_dict_list[-1][0]
    else:
        root_expr = mod_isr

    dtype = float
    if gamma_val != 0.0:
        dtype = complex
    res_tens = np.zeros((3,)*sos.order, dtype=dtype)

    if isinstance(root_expr, Add):
        term_list = [arg for arg in root_expr.args]
    else:
        term_list = [root_expr]
    
    if symmetric:
        components = list(combinations_with_replacement([0, 1, 2], sos.order)) # if tensor is symmetric
    else:
        components = list(product([0, 1, 2], repeat=sos.order))
    for c in components:
        comp_map = {
                ABC[ic]: cc for ic, cc in enumerate(c)
        }
        #subs_dict = {o[0]: o[1] for o in all_omegas}
        #subs_dict[gamma] = gamma_val
        
        for term in term_list:
            subs_dict = {o[0]: o[1] for o in all_omegas}
            subs_dict[gamma] = gamma_val
            for i, a in enumerate(term.args):
                oper_a = a
                if isinstance(a, adjoint):
                    oper_a = a.args[0]
                if isinstance(oper_a, ResponseVector) and oper_a == a: # vec * X
                    lhs = term.args[i-1]
                    if isinstance(lhs, S2S_MTM): # from_vec * B * X --> transition polarizability
                        dips = np.array(adcc_prop_dict[lhs.op_type].dips)
                        lhs2 = term.args[i-2]
                        key = lhs2*lhs*a
                        if isinstance(lhs2, adjoint) and isinstance(lhs2.args[0], ResponseVector): # Dagger(X) * B * X
                            comps_rvecl = tuple([comp_map[char] for char in list(lhs2.args[0].comp)])
                            # for response vectors that were computed from the B matrix, the symmetry has already been taken into account
                            if lhs2.args[0].mtm_type == str(S2S_MTM) or lhs2.args[0].symmetry == 1: # Hermitian operators
                                from_v = response_dict[lhs2.args[0].no][comps_rvecl]
                            elif lhs2.args[0].symmetry == 2: # anti-Hermitian operators
                                from_v = -1.0 * response_dict[lhs2.args[0].no][comps_rvecl]
                            else:
                                raise NotImplementedError("Only Hermitian and anti-Hermitian operators are implemented.")
                        elif isinstance(lhs2, Bra): # <f| * B * X
                            assert lhs2.label[0] == final_state[0]
                            from_v = state.excitation_vector[final_state[1]]
                        else:
                            raise ValueError("Expression cannot be evaluated.")
                        comps_rvecr = tuple([comp_map[char] for char in list(oper_a.comp)])
                        to_v = response_dict[oper_a.no][comps_rvecr]
                        comps_dip = tuple([comp_map[char] for char in list(lhs.comp)])
                        if isinstance(from_v, AmplitudeVector) and isinstance(to_v, AmplitudeVector):
                            subs_dict[key] = transition_polarizability(
                                    property_method, mp, from_v, dips[comps_dip], to_v
                            )
                        else:
                            if isinstance(from_v, AmplitudeVector):
                                from_v = RV(from_v)
                            subs_dict[key] = transition_polarizability_complex(
                                    property_method, mp, from_v, dips[comps_dip], to_v
                            )
                    elif isinstance(lhs, adjoint) and isinstance(lhs.args[0], MTM): # Dagger(F) * X
                        if lhs.args[0].symmetry == 1: # Hermitian operators
                            mtms = np.array(adcc_prop_dict[lhs.args[0].op_type].mtms)
                        elif lhs.args[0].symmetry == 2: # anti-Hermitian operators
                            mtms = -1.0 * np.array(adcc_prop_dict[lhs.args[0].op_type].mtms)
                        else:
                            raise NotImplementedError("Only Hermitian and anti-Hermitian operators are implemented.")
                        comps_rvec = tuple([comp_map[char] for char in list(oper_a.comp)])
                        comps_mtm = tuple([comp_map[char] for char in list(lhs.args[0].comp)])
                        subs_dict[lhs*a] = from_vec_to_vec(
                                mtms[comps_mtm], response_dict[oper_a.no][comps_rvec]
                        )
                    elif isinstance(lhs, adjoint) and isinstance(lhs.args[0], ResponseVector): # Dagger(X) * X
                        # for response vectors that were computed from the B matrix, the symmetry has already been taken into account
                        comps_rvecl = tuple([comp_map[char] for char in list(lhs.args[0].comp)])
                        if lhs.args[0].mtm_type == str(S2S_MTM) or lhs.args[0].symmetry == 1: # Hermitian operators
                            left_rvec = response_dict[lhs.args[0].no][comps_rvecl]
                        elif lhs.args[0].symmetry == 2: # anti-Hermitian operators
                            left_rvec = -1.0 * response_dict[lhs.args[0].no][comps_rvecl]
                        else:
                             raise NotImplementedError("Only Hermitian and anti-Hermitian operators are implemented.")
                        comps_rvecr = tuple([comp_map[char] for char in list(oper_a.comp)])
                        subs_dict[lhs*a] = from_vec_to_vec(
                                left_rvec, response_dict[oper_a.no][comps_rvecr]
                        )
                    else:
                        raise ValueError("Expression cannot be evaluated.")
                elif isinstance(oper_a, ResponseVector) and oper_a != a:
                    rhs = term.args[i+1]
                    comps_rvec = tuple([comp_map[char] for char in list(oper_a.comp)])
                    # for response vectors that were computed from the B matrix, the symmetry has already been taken into account
                    if oper_a.mtm_type == str(S2S_MTM) or oper_a.symmetry == 1: # Hermitian operators
                        from_v = response_dict[oper_a.no][comps_rvec]
                    elif oper_a.symmetry == 2: # anti-Hermitian operators
                        from_v = -1.0 * response_dict[oper_a.no][comps_rvec]
                    else:
                         raise NotImplementedError("Only Hermitian and anti-Hermitian operators are implemented.")

                    if isinstance(rhs, S2S_MTM): # Dagger(X) * B * to_vec --> transition polarizability
                        dips = np.array(adcc_prop_dict[rhs.op_type].dips)
                        rhs2 = term.args[i+2]
                        key = a*rhs*rhs2
                        if isinstance(rhs2, ResponseVector): # Dagger(X) * B * X (taken care of above)
                            continue
                        elif isinstance(rhs2, Ket): # Dagger(X) * B * |f>
                            assert rhs2.label[0] == final_state[0]
                            to_v = state.excitation_vector[final_state[1]]
                        else:
                            raise ValueError("Expression cannot be evaluated.")
                        comps_dip = tuple([comp_map[char] for char in list(rhs.comp)])
                        if isinstance(from_v, AmplitudeVector) and isinstance(to_v, AmplitudeVector):
                            subs_dict[key] = transition_polarizability(
                                    property_method, mp, from_v, dips[comps_dip], to_v
                            )
                        else:
                            to_v = RV(to_v)
                            subs_dict[key] = transition_polarizability_complex(
                                    property_method, mp, from_v, dips[comps_dip], to_v
                            )
                    elif isinstance(rhs, MTM): # Dagger(X) * F
                        mtms = np.array(adcc_prop_dict[rhs.op_type].mtms)
                        comps_mtm = tuple([comp_map[char] for char in list(rhs.comp)])
                        subs_dict[a*rhs] = from_vec_to_vec(
                                from_v, mtms[comps_mtm]
                        )
                    elif isinstance(rhs, ResponseVector): # Dagger(X) * X (taken care of above)
                        continue
                    else:
                        raise ValueError("Expression cannot be evaluated.")

                elif isinstance(a, DipoleMoment):
                    comps_dipmom = tuple([comp_map[char] for char in list(a.comp)])
                    if a.from_state == "0" and a.to_state == "0":
                        gs_dip_moment = adcc_prop_dict[a.op_type].gs_dip_moment
                        subs_dict[a] = gs_dip_moment[comps_dipmom]
                    elif a.from_state == "0" and a.to_state == str(final_state[0]):
                        tdms = adcc_prop_dict[a.op_type].transition_dipole_moment
                        subs_dict[a] = tdms[final_state[1]][comps_dipmom]
                    else:
                        raise ValueError("Unknown dipole moment.")
                elif isinstance(a, LeviCivita):
                    subs_dict[a] = lc_tensor[c]
            res = term.subs(subs_dict)
            if res == zoo:
                raise ZeroDivisionError()
            res_tens[c] += res
        #print(root_expr, subs_dict)
        #res = root_expr.subs(subs_dict)
        #print(res)
        #if res == zoo:
        #    raise ZeroDivisionError()
        #res_tens[c] = res
        if symmetric:
            perms = list(permutations(c)) # if tensor is symmetric
            for p in perms:
                res_tens[p] = res_tens[c]
    return res_tens


def evaluate_property_sos(
        state, sos_expr, summation_indices, omegas=None, gamma_val=0.0,
        final_state=None, perm_pairs=None, extra_terms=True, symmetric=False, excluded_cases=None
    ):
    """Compute a molecular property from its SOS expression.

    Parameters
    ----------
    state: <class 'adcc.ExcitedStates.ExcitedStates'>
        ExcitedStates object returned by an ADC calculation that includes all states of the system.

    sos_expr: <class 'sympy.core.add.Add'> or <class 'sympy.core.mul.Mul'>
        SymPy expression of the SOS;
        it can be either the full expression or a single term from which the full expression can be generated via permutation.

    summation_indices: list of <class 'sympy.core.symbol.Symbol'>
        List of indices of summation.

    omegas: list of tuples, optional
        List of (symbol, value) pairs for the frequencies;
        (symbol, value): (<class 'sympy.core.symbol.Symbol'>, <class 'sympy.core.add.Add'> or <class 'sympy.core.symbol.Symbol'> or float),
        e.g., [(w_o, w_1+w_2), (w_1, 0.5), (w_2, 0.5)].

    gamma_val: float, optional

    final_state: tuple, optional
        (<class 'sympy.core.symbol.Symbol'>, int), e.g., (f, 0).

    perm_pairs: list of tuples, optional
        List of (op, freq) pairs whose permutation yields the full SOS expression;
        (op, freq): (<class 'responsetree.response_operators.DipoleOperator'>, <class 'sympy.core.symbol.Symbol'>),
        e.g., [(op_a, -w_o), (op_b, w_1), (op_c, w_2)].

    extra_terms: bool, optional
        Compute the additional terms that arise when converting the SOS expression to its ADC/ISR formulation;
        by default 'True'.

    symmetric: bool, optional
        Resulting tensor is symmetric; 
        by default 'False'.

    Returns
    ----------
    <class 'numpy.ndarray'>
        Resulting tensor.
    """
    if omegas is None:
        omegas = []
    elif type(omegas) == tuple:
        omegas = [omegas]
    else:
        assert type(omegas) == list
    assert type(extra_terms) == bool
    assert type(symmetric) == bool
    if excluded_cases is None:
        excluded_cases = []
    
    correlation_btw_freq = [tup for tup in omegas if type(tup[1]) == Symbol or type(tup[1]) == Add]
    all_omegas = omegas.copy()
    if final_state:
        assert type(final_state) == tuple and len(final_state) == 2
        all_omegas.append(
                (TransitionFrequency(str(final_state[0]), real=True),
                state.excitation_energy_uncorrected[final_state[1]])
        )
    else:
        assert final_state is None
    sos = SumOverStates(
            sos_expr, summation_indices, correlation_btw_freq=correlation_btw_freq, perm_pairs=perm_pairs, excluded_cases=excluded_cases
    )
    adcc_prop_dict = {}
    for op_type in sos.operator_types:
        adcc_prop_dict[op_type] = AdccProperties(state, op_type)

    _check_omegas_and_final_state(sos.expr, omegas, sos.correlation_btw_freq, gamma_val, final_state)
    
    # all terms are stored as dictionaries in a list
    if isinstance(sos.expr, Add):
        term_list = [
                {"expr": term, "summation_indices": sos.summation_indices, "transition_frequencies": sos.transition_frequencies}
                for term in sos.expr.args
        ]
    else:
        term_list = [
                {"expr": sos.expr, "summation_indices": sos.summation_indices, "transition_frequencies": sos.transition_frequencies}
        ]
    if extra_terms:
        ets = compute_extra_terms(
                sos.expr, sos.summation_indices, excluded_cases=sos.excluded_cases, correlation_btw_freq=sos.correlation_btw_freq
        )
        if isinstance(ets, Add):
            et_list = list(ets.args)
        elif isinstance(ets, Mul):
            et_list = [ets]
        else:
            et_list = []
        for et in et_list:
            sum_ind = find_remaining_indices(et, sos.summation_indices) # the extra terms contain less indices of summation
            trans_freq = [TransitionFrequency(str(index), real=True) for index in sum_ind]
            term_list.append(
                    {"expr": et, "summation_indices": sum_ind, "transition_frequencies": trans_freq}
            )
    
    dtype = float
    if gamma_val != 0.0:
        dtype = complex
    res_tens = np.zeros((3,)*sos.order, dtype=dtype)
    
    if symmetric:
        components = list(combinations_with_replacement([0, 1, 2], sos.order)) # if tensor is symmetric
    else:
        components = list(product([0, 1, 2], repeat=sos.order))
    
    modified_excluded_cases = [
            (str(tup[0]), final_state[1]) if tup[1] == final_state[0] else (str(tup[0]), tup[1]) for tup in excluded_cases
    ]

    for term_dict in tqdm(term_list):
        mod_expr = replace_bra_op_ket(
                term_dict["expr"].subs(sos.correlation_btw_freq)
        )
        sum_ind_str = [str(si) for si in term_dict["summation_indices"]]
        
        # values that the indices of summation can take on
        indices = list(
                product(range(len(state.excitation_energy_uncorrected)), repeat=len(term_dict["summation_indices"]))
        )
        dip_mom_list = [a for a in mod_expr.args if isinstance(a, DipoleMoment)]
        lc_contained = False
        for a in mod_expr.args:
            if isinstance(a, LeviCivita):
                lc_contained = True
        for i in indices:
            state_map = {
                    sum_ind_str[ii]: ind for ii, ind in enumerate(i)
                }
            
            # skip the rest of the loop for this iteration if it corresponds to one of the excluded cases
            if set(modified_excluded_cases).intersection(set(state_map.items())):
                continue

            if final_state:
                state_map[str(final_state[0])] = final_state[1]
            for c in components:
                comp_map = {
                        ABC[ic]: cc for ic, cc in enumerate(c)
                }
                subs_dict = {o[0]: o[1] for o in all_omegas}
                subs_dict[gamma] = gamma_val
                
                for si, tf in zip(sum_ind_str, term_dict["transition_frequencies"]):
                    subs_dict[tf] = state.excitation_energy_uncorrected[state_map[si]]

                for a in dip_mom_list:
                    comps_dipmom = tuple([comp_map[char] for char in list(a.comp)])
                    if a.from_state == "0" and a.to_state == "0":
                        gs_dip_moment = adcc_prop_dict[a.op_type].gs_dip_moment
                        subs_dict[a] = gs_dip_moment[comps_dipmom]
                    elif a.from_state == "0":
                        index = state_map[a.to_state]
                        tdms = adcc_prop_dict[a.op_type].transition_dipole_moment
                        subs_dict[a] = tdms[index][comps_dipmom]
                    elif a.to_state == "0":
                        index = state_map[a.from_state]
                        tdms = adcc_prop_dict[a.op_type].transition_dipole_moment
                        if a.symmetry == 1: # Hermitian operators
                            subs_dict[a] = tdms[index][comps_dipmom]
                        elif a.symmetry == 2: # anti-Hermitian operators
                            subs_dict[a] = -1.0 * tdms[index][comps_dipmom] # TODO: correct sign?
                        else:
                            raise NotImplementedError("Only Hermitian and anti-Hermitian operators are implemented.")
                    else:
                        index1 = state_map[a.from_state]
                        index2 = state_map[a.to_state]
                        if a.from_state in sum_ind_str and a.to_state in sum_ind_str: # e.g., <n|\mu|m>
                            s2s_tdms = adcc_prop_dict[a.op_type].state_to_state_transition_moment
                            subs_dict[a] = s2s_tdms[index1, index2][comps_dipmom]
                        elif a.from_state in sum_ind_str: # e.g., <f|\mu|n>
                            s2s_tdms_f = adcc_prop_dict[a.op_type].s2s_tm(final_state=index2)
                            subs_dict[a] = s2s_tdms_f[index1][comps_dipmom]
                        elif a.to_state in sum_ind_str: # e.g., <n|\mu|f>
                            s2s_tdms_f = adcc_prop_dict[a.op_type].s2s_tm(initial_state=index1)
                            subs_dict[a] = s2s_tdms_f[index2][comps_dipmom]
                        else:
                            raise ValueError()
                if lc_contained:
                    subs_dict[LeviCivita()] = lc_tensor[c]
                res = mod_expr.xreplace(subs_dict)
                if res == zoo:
                    raise ZeroDivisionError()
                res_tens[c] += res
                if symmetric:
                    perms = list(permutations(c)) # if tensor is symmetric
                    for p in perms:
                        res_tens[p] = res_tens[c]
    return res_tens


def evaluate_property_sos_fast(
        state, sos_expr, summation_indices, omegas=None, gamma_val=0.0,
        final_state=None, perm_pairs=None, extra_terms=True, excluded_cases=None
    ):
    """Compute a molecular property from its SOS expression using the Einstein summation convention.

    Parameters
    ----------
    state: <class 'adcc.ExcitedStates.ExcitedStates'>
        ExcitedStates object returned by an ADC calculation that includes all states of the system.

    sos_expr: <class 'sympy.core.add.Add'> or <class 'sympy.core.mul.Mul'>
        SymPy expression of the SOS;
        it can be either the full expression or a single term from which the full expression can be generated via permutation.
        It already includes the additional terms.

    summation_indices: list of <class 'sympy.core.symbol.Symbol'>
        List of indices of summation.

    omegas: list of tuples, optional
        List of (symbol, value) pairs for the frequencies;
        (symbol, value): (<class 'sympy.core.symbol.Symbol'>, <class 'sympy.core.add.Add'> or <class 'sympy.core.symbol.Symbol'> or float),
        e.g., [(w_o, w_1+w_2), (w_1, 0.5), (w_2, 0.5)].

    gamma_val: float, optional

    final_state: tuple, optional
        (<class 'sympy.core.symbol.Symbol'>, int), e.g., (f, 0).

    perm_pairs: list of tuples, optional
        List of (op, freq) pairs whose permutation yields the full SOS expression;
        (op, freq): (<class 'responsetree.response_operators.DipoleOperator'>, <class 'sympy.core.symbol.Symbol'>),
        e.g., [(op_a, -w_o), (op_b, w_1), (op_c, w_2)].

    extra_terms: bool, optional
        Compute the additional terms that arise when converting the SOS expression to its ADC/ISR formulation;
        by default 'True'.

    Returns
    ----------
    <class 'numpy.ndarray'>
        Resulting tensor.
    """
    if omegas is None:
        omegas = []
    elif type(omegas) == tuple:
        omegas = [omegas]
    else:
        assert type(omegas) == list
    assert type(extra_terms) == bool

    correlation_btw_freq = [tup for tup in omegas if type(tup[1]) == Symbol or type(tup[1]) == Add]
    subs_dict = {om_tup[0]: om_tup[1] for om_tup in omegas}
    if final_state:
        assert type(final_state) == tuple and len(final_state) == 2
        subs_dict[TransitionFrequency(str(final_state[0]), real=True)] = (
            state.excitation_energy_uncorrected[final_state[1]]
        )
    else:
        assert final_state is None
    subs_dict[gamma] = gamma_val
    sos = SumOverStates(
            sos_expr, summation_indices, correlation_btw_freq=correlation_btw_freq, perm_pairs=perm_pairs, excluded_cases=excluded_cases
    )
    adcc_prop_dict = {}
    for op_type in sos.operator_types:
        adcc_prop_dict[op_type] = AdccProperties(state, op_type)

    _check_omegas_and_final_state(sos.expr, omegas, correlation_btw_freq, gamma_val, final_state)

    if extra_terms:
        sos_with_et = sos.expr + compute_extra_terms(
                sos.expr, sos.summation_indices, excluded_cases=sos.excluded_cases, correlation_btw_freq=sos.correlation_btw_freq
        )
        sos_expr_mod = sos_with_et.subs(correlation_btw_freq)
    else:
        sos_expr_mod = sos.expr.subs(correlation_btw_freq)

    dtype = float
    if gamma_val != 0.0:
        dtype = complex
    res_tens = np.zeros((3,)*sos.order, dtype=dtype)

    if isinstance(sos_expr_mod, Add):
        term_list = [replace_bra_op_ket(arg) for arg in sos_expr_mod.args]
    else:
        term_list = [replace_bra_op_ket(sos_expr_mod)]
    
    for term in term_list:
        einsum_list = []
        factor = 1
        divergences = []
        for a in term.args:
            if isinstance(a, DipoleMoment):
                if a.from_state == "0" and a.to_state == "0": # <0|\mu|0>
                    gs_dip_moment = adcc_prop_dict[a.op_type].gs_dip_moment
                    einsum_list.append(("", a.comp, gs_dip_moment))
                elif a.from_state == "0":
                    tdms = adcc_prop_dict[a.op_type].transition_dipole_moment
                    if a.to_state in sos.summation_indices_str: # e.g., <n|\mu|0>
                        einsum_list.append((a.to_state, a.comp, tdms))
                    else: # e.g., <f|\mu|0>
                        einsum_list.append(("", a.comp, tdms[final_state[1]]))
                elif a.to_state == "0":
                    if a.symmetry == 1: # Hermitian operators
                        tdms = adcc_prop_dict[a.op_type].transition_dipole_moment
                    elif a.symmetry == 2: # anti-Hermitian operators
                        tdms = -1.0 * adcc_prop_dict[a.op_type].transition_dipole_moment # TODO: correct sign?
                    else:
                        raise NotImplementedError("Only Hermitian and anti-Hermitian operators are implemented.")
                    if a.from_state in sos.summation_indices_str: # e.g., <0|\mu|n>
                        einsum_list.append((a.from_state, a.comp, tdms))
                    else: # e.g., <0|\mu|f>
                        einsum_list.append(("", a.comp, tdms[final_state[1]]))
                else:
                    if a.from_state in sos.summation_indices_str and a.to_state in sos.summation_indices_str: # e.g., <n|\mu|m>
                        s2s_tdms = adcc_prop_dict[a.op_type].state_to_state_transition_moment
                        einsum_list.append((a.from_state+a.to_state, a.comp, s2s_tdms))
                    elif a.from_state in sos.summation_indices_str and a.to_state == str(final_state[0]): # e.g., <f|\mu|n>
                        s2s_tdms_f = adcc_prop_dict[a.op_type].s2s_tm(final_state=final_state[1])
                        einsum_list.append((a.from_state, a.comp, s2s_tdms_f))
                    elif a.to_state in sos.summation_indices_str and a.from_state == str(final_state[0]): # e.g., <n|\mu|f>
                        s2s_tdms_f = adcc_prop_dict[a.op_type].s2s_tm(initial_state=final_state[1])
                        einsum_list.append((a.to_state, a.comp, s2s_tdms_f))
                    else:
                        raise ValueError()

            elif isinstance(a, Pow):
                pow_expr = a.args[0].subs(subs_dict)
                if pow_expr == 0:
                    raise ZeroDivisionError()
                index = None
                shift = 0
                if isinstance(pow_expr, Add):
                    pow_expr_list = [arg for arg in pow_expr.args]
                else:
                    pow_expr_list = [pow_expr]
                for aa in pow_expr_list:
                    if aa in sos.transition_frequencies:
                        index = aa.state
                    elif isinstance(aa, Float) or isinstance(aa, Integer):
                        # convert SymPy object to float
                        shift += float(aa)
                    elif isinstance(aa, Mul) and aa.args[1] is I:
                        shift += 1j*float(aa.args[0])
                    else:
                        raise ValueError()
                if index is None:
                    if shift:
                        einsum_list.append(("", "", 1/(shift)))
                    else:
                        raise ZeroDivisionError()
                else:
                    array = 1/(state.excitation_energy_uncorrected + shift)
                    if np.inf in array:
                        index_with_inf = np.where(array ==  np.inf)
                        assert len(index_with_inf) == 1
                        assert len(index_with_inf[0]) == 1
                        divergences.append((Symbol(index, real=True), index_with_inf[0][0]))
                    einsum_list.append((index, "", array))
            
            elif isinstance(a, LeviCivita):
                einsum_list.append(("", "ABC", lc_tensor))

            elif isinstance(a, Integer) or isinstance(a, Float):
                factor *= float(a)

            else:
                raise TypeError(f"The following type was not recognized: {type(a)}.")
        
        if len(divergences) != 0:
            print("The following divergences have been found (explaining the RuntimeWarning): ", divergences)
        einsum_left = ""
        einsum_right = ""
        array_list = []
        removed_divergences = []
        # create string of subscript labels and list of np.arrays for np.einsum
        for tup in einsum_list:
            state_str, comp_str, array = tup
            einsum_left += state_str + comp_str + ","
            einsum_right += comp_str
            # remove excluded cases from corresponding arrays
            if excluded_cases and state_str:
                for case in excluded_cases:
                    if str(case[0]) in state_str and case[1] != O:
                        assert case[1] == final_state[0]
                        index_to_delete = final_state[1]
                        axis = state_str.index(str(case[0]))
                        array = np.delete(array, index_to_delete, axis=axis)
                        removed_divergences.append((case[0], final_state[1]))
            array_list.append(array)
        removed_divergences = list(set(removed_divergences))
        divergences_copied = divergences.copy()
        for rd in removed_divergences:
            if rd not in divergences:
                raise ValueError(
                        "A case that did not cause any divergences was excluded from the summation.\n"
                        "Please check the excluded_cases list that was passed to the function."
                )
            divergences_copied.remove(rd)
        if len(divergences) != 0:
            if len(divergences_copied) != 0:
                raise ZeroDivisionError(f"Not all divergences that occured could be eliminated. The following divergences remain: {divergences}.")
            else:
                print("However, all of these divergences have been successfully removed.")
        einsum_left_mod = einsum_left[:-1]
        einsum_right_list = list(set(einsum_right))
        einsum_right_list.sort()
        einsum_right_mod = ''.join(einsum_right_list)
        einsum_string = einsum_left_mod + " -> " + einsum_right_mod
        print("Created string of subscript labels that is used by np.einsum:\n", einsum_string)
        res_tens += (factor * np.einsum(einsum_string, *array_list))
    
    return res_tens


if __name__ == "__main__":
    from pyscf import gto, scf
    import adcc
    from responsefun.testdata import cache
    from adcc.Excitation import Excitation
    from respondo.polarizability import static_polarizability, real_polarizability, complex_polarizability
    from respondo.rixs import rixs_scattering_strength, rixs
    from respondo.tpa import tpa_resonant
    import time
    from responsefun.test_property import SOS_expressions

    mol = gto.M(
        atom="""
        O 0 0 0
        H 0 0 1.795239827225189
        H 1.693194615993441 0 -0.599043184453037
        """,
        unit="Bohr",
        basis="sto-3g",
    )

    scfres = scf.RHF(mol)
    scfres.kernel()

    refstate = adcc.ReferenceState(scfres)
    matrix = adcc.AdcMatrix("adc2", refstate)
    state = adcc.adc2(scfres, n_singlets=5)
    mock_state = cache.data_fulldiag["h2o_sto3g_adc2"] 
    
    alpha_term = SOS_expressions['alpha_complex'][0]
    omega_alpha = [(w, 0.5)]
    gamma_val = 0.01
    #alpha_tens = evaluate_property_isr(state, alpha_term, [n], omega_alpha, gamma_val=gamma_val)
    #print(alpha_tens)
    #alpha_tens_ref = complex_polarizability(refstate, "adc2", 0.5, gamma_val)
    #print(alpha_tens_ref)
    
    gamma_term = (
            TransitionMoment(O, op_a, n) * TransitionMoment(n, op_b, m) * TransitionMoment(m, op_c, p) * TransitionMoment(p, op_d, O)
            / ((w_n - w_o) * (w_m - w_2 - w_3) * (w_p - w_3))
    )
    gamma_omegas = [(w_1, 0.5), (w_2, 0.55), (w_3, 0.6), (w_o, w_1+w_2+w_3)]
    #gamma_tens1 = (
    #        evaluate_property_isr(state, gamma_term, [m, n, p], gamma_omegas, extra_terms=False)
    #)
    #print(gamma_tens1)
    #gamma_tens1_sos = (
    #        evaluate_property_sos_fast(mock_state, gamma_term, [m, n, p], gamma_omegas, extra_terms=False)
    #)
    #print(gamma_tens1_sos)
    #np.testing.assert_allclose(gamma_tens1, gamma_tens1_sos, atol=1e-6)

    
    threepa_term = (
            TransitionMoment(O, op_a, m) * TransitionMoment(m, op_b, n) * TransitionMoment(n, op_c, f)
            / ((w_n - w_1 - w_2) * (w_m - w_1))
    )
    #threepa_perm_pairs = [(op_a, w_1), (op_b, w_2), (op_c, w_3)]
    #threepa_omegas = [
    #        (w_1, state.excitation_energy[0]/3),
    #        (w_2, state.excitation_energy[0]/3),
    #        (w_3, state.excitation_energy[0]/3),
    #        (w_1, w_f-w_2-w_3)
    #]
    #threepa_tens = (
    #        evaluate_property_isr(state, threepa_term, [m, n], threepa_omegas, perm_pairs=threepa_perm_pairs, final_state=(f, 0))
    #)
    #print(threepa_tens)
    #threepa_term = (
    #        TransitionMoment(O, op_a, m) * TransitionMoment(m, op_b, n) * TransitionMoment(n, op_c, f)
    #        / ((w_n - 2*(w_f/3)) * (w_m - (w_f/3)))
    #)
    #threepa_perm_pairs = [(op_a, w), (op_b, w), (op_c, w)]
    #threepa_omegas = [
    #        #(w, state.excitation_energy[0]/3),
    #        #(w, w_f/3)
    #]
    #threepa_tens = (
    #        evaluate_property_isr(state, threepa_term, [m, n], threepa_omegas, perm_pairs=threepa_perm_pairs, final_state=(f, 0))
    #)
    #print(threepa_tens)

    #threepa_tens_sos = (
    #        evaluate_property_sos_fast(state, threepa_term, [m, n], threepa_omegas, perm_pairs=threepa_perm_pairs, final_state=(f, 0))
    #)
    #print(threepa_tens_sos)
    #np.testing.assert_allclose(threepa_tens, threepa_tens_sos, atol=1e-6)

    # TODO: make it work for esp also in the static case --> projecting the fth eigenstate out of the matrix
    omega_alpha = [(w, 0.5)]
    esp_terms = (
        TransitionMoment(f, op_a, n) * TransitionMoment(n, op_b, f) / (w_n - w_f - w - 1j*gamma)
        + TransitionMoment(f, op_b, n) * TransitionMoment(n, op_a, f) / (w_n - w_f + w + 1j*gamma)
    )
    #esp_tens = evaluate_property_isr(
    #        state, esp_terms, [n], omega_alpha, 0.0/Hartree, final_state=(f, 0)#, excluded_cases=[(n, f)]
    #)
    #print(esp_tens)
    #esp_tens_sos = evaluate_property_sos_fast(
    #        mock_state, esp_terms, [n], omega_alpha, 0.0/Hartree, final_state=(f, 0)#, excluded_cases=[(n, f)]
    #)
    #print(esp_tens_sos)
    #np.testing.assert_allclose(esp_tens, esp_tens_sos, atol=1e-7)

    epsilon = LeviCivita()
    mcd_term1 = (
            -1.0 * epsilon
            * TransitionMoment(O, opm_b, k) * TransitionMoment(k, op_c, f) * TransitionMoment(f, op_a, O)
            / w_k
    )
    #mcd_tens1 = evaluate_property_isr(
    #        state, mcd_term1, [k], final_state=(f, 0), extra_terms=False
    #)
    #print(mcd_tens1)
    #mcd_tens1_sos = evaluate_property_sos_fast(
    #        state, mcd_term1, [k], final_state=(f, 0), extra_terms=False, excluded_cases=[(k, O)]
    #)
    #print(mcd_tens1_sos)
    #mcd_tens1_sos2 = evaluate_property_sos(
    #        state, mcd_term1, [k], final_state=(f, 0), extra_terms=False, excluded_cases=[(k, O)]
    #)
    #print(mcd_tens1_sos2)
    #np.testing.assert_allclose(mcd_tens1, mcd_tens1_sos, atol=1e-7)
    #np.testing.assert_allclose(mcd_tens1_sos, mcd_tens1_sos2, atol=1e-7)
    #mcd_term2 = (
    #        -1.0 * epsilon
    #        * TransitionMoment(O, op_c, k) * TransitionMoment(k, opm_b, f) * TransitionMoment(f, op_a, O)
    #        / (w_k - w_f)
    #)
    ##mcd_tens2 = evaluate_property_isr(
    ##        state, mcd_term2, [k], final_state=(f, 0), extra_terms=False
    ##)
    ##print(mcd_tens2)
    #mcd_tens2_sos = evaluate_property_sos_fast(
    #        mock_state, mcd_term2, [k], final_state=(f, 0), extra_terms=False, excluded_cases=[(k, f)]
    #)
    #print(mcd_tens2_sos)
    #mcd_tens2_sos2 = evaluate_property_sos(
    #        mock_state, mcd_term2, [k], final_state=(f, 0), extra_terms=False, excluded_cases=[(k, f)]
    #)
    #print(mcd_tens2_sos2)
    #np.testing.assert_allclose(mcd_tens2_sos, mcd_tens2_sos2, atol=1e-7)
    #mcd_tens = mcd_tens1_sos+mcd_tens2_sos
    #mcd_tens2 = mcd_tens1_sos2+mcd_tens2_sos2
    #print(mcd_tens, mcd_tens2)
    #np.testing.assert_allclose(mcd_tens, mcd_tens2)
    
    #excited_state = Excitation(state, 0, "adc2")
    #mcd_ref = mcd_bterm(excited_state)
    #print(mcd_ref)

    gamma_extra_term = (
            TransitionMoment(O, op_a, n) * TransitionMoment(n, op_b, O) * TransitionMoment(O, op_c, m) * TransitionMoment(m, op_d, O)
            / ((w_n - w_o) * (w_m - w_3) * (w_m + w_2))
    )
    #gamma_extra_tens = evaluate_property_isr(
    #        state, gamma_extra_term, [n, m], omegas=[(w_1, 0.5), (w_2, 0.6), (w_3, 0.7), (w_o, w_1+w_2+w_3)],
    #        perm_pairs=[(op_a, -w_o), (op_b, w_1), (op_c, w_2), (op_d, w_3)],
    #        extra_terms=False
    #)
    #print(gamma_extra_tens)

    esp_extra_terms =  (
        TransitionMoment(f, op_a, O) * TransitionMoment(O, op_b, f) / (- w_f - w - 1j*gamma)
        + TransitionMoment(f, op_b, O) * TransitionMoment(O, op_a, f) / (- w_f + w + 1j*gamma)
    )
    #esp_extra_tens = evaluate_property_isr(
    #        state, esp_extra_terms, [], omegas=[(w, 0.5)], gamma_val=0.01, final_state=(f, 2), extra_terms=False
    #)
    #print(esp_extra_tens)
