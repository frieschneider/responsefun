import adcc
import numpy as np
import pytest
from scipy.constants import physical_constants

from responsefun.evaluate_property import (
    evaluate_property_isr,
    evaluate_property_sos_fast,
)
from responsefun.SumOverStates import TransitionMoment
from responsefun.symbols_and_labels import (
    O,
    Q_ab,
    Q_bc,
    Q_cd,
    Q_de,
    k,
    m,
    n,
    op_a,
    op_b,
    op_c,
    op_d,
    op_e,
    p,
    w,
    w_2,
    w_3,
    w_k,
    w_m,
    w_n,
    w_o,
    w_p,
)
from responsefun.testdata import cache
from responsefun.testdata.static_data import xyz

Hartree = physical_constants["hartree-electron volt relationship"][0]


def run_scf(molecule, basis, backend="pyscf"):
    scfres = adcc.backends.run_hf(
        backend,
        xyz=xyz[molecule],
        basis=basis,
    )
    return scfres


SOS_alpha_like = {
    "ab": (
        TransitionMoment(O, Q_ab, n) * TransitionMoment(n, op_c, O) / (w_n - w)
        + TransitionMoment(O, op_c, n) * TransitionMoment(n, Q_ab, O) / (w_n + w)
    ),
    "bc": (
        TransitionMoment(O, op_a, n) * TransitionMoment(n, Q_bc, O) / (w_n - w)
        + TransitionMoment(O, Q_bc, n) * TransitionMoment(n, op_a, O) / (w_n + w)
    ),
    "abcd": (
        TransitionMoment(O, Q_ab, n) * TransitionMoment(n, Q_cd, O) / (w_n - w)
        + TransitionMoment(O, Q_cd, n) * TransitionMoment(n, Q_ab, O) / (w_n + w)
    ),
}


SOS_beta_like = {
    "ab": (
        TransitionMoment(O, Q_ab, n)
        * TransitionMoment(n, op_c, k)
        * TransitionMoment(k, op_d, O)
        / ((w_n - w_o) * (w_k - w_2))
    ),
    "bc": (
        TransitionMoment(O, op_a, n)
        * TransitionMoment(n, Q_bc, k)
        * TransitionMoment(k, op_d, O)
        / ((w_n - w_o) * (w_k - w_2))
    ),
    "cd": (
        TransitionMoment(O, op_a, n)
        * TransitionMoment(n, op_b, k)
        * TransitionMoment(k, Q_cd, O)
        / ((w_n - w_o) * (w_k - w_2))
    ),
    "abde": (
        TransitionMoment(O, Q_ab, n)
        * TransitionMoment(n, op_c, k)
        * TransitionMoment(k, Q_de, O)
        / ((w_n - w_o) * (w_k - w_2))
    ),
    "bcde": (
        TransitionMoment(O, op_a, n)
        * TransitionMoment(n, Q_bc, k)
        * TransitionMoment(k, Q_de, O)
        / ((w_n - w_o) * (w_k - w_2))
    ),
}


SOS_gamma_like = {
    "ab": (
        TransitionMoment(O, Q_ab, n)
        * TransitionMoment(n, op_c, m)
        * TransitionMoment(m, op_d, p)
        * TransitionMoment(p, op_e, O)
        / ((w_n - w_o) * (w_m - w_2 - w_3) * (w_p - w_3))
    ),
    "bc": (
        TransitionMoment(O, op_a, n)
        * TransitionMoment(n, Q_bc, m)
        * TransitionMoment(m, op_d, p)
        * TransitionMoment(p, op_e, O)
        / ((w_n - w_o) * (w_m - w_2 - w_3) * (w_p - w_3))
    ),
    "de": (
        TransitionMoment(O, op_a, n)
        * TransitionMoment(n, op_b, m)
        * TransitionMoment(m, op_c, p)
        * TransitionMoment(p, Q_de, O)
        / ((w_n - w_o) * (w_m - w_2 - w_3) * (w_p - w_3))
    ),
}


@pytest.mark.parametrize("ops", SOS_alpha_like.keys())
class TestAlphaLike:
    def test_h2o_sto3g_adc2(self, ops):
        case = "h2o_sto3g_adc2"
        if case not in cache.data_fulldiag:
            pytest.skip(f"{case} cache file not available.")
        molecule, basis, method = case.split("_")
        scfres = run_scf(molecule, basis)
        refstate = adcc.ReferenceState(scfres)
        expr = SOS_alpha_like[ops]
        mock_state = cache.data_fulldiag[case]
        state = adcc.run_adc(refstate, method=method, n_singlets=5)

        alpha_sos = evaluate_property_sos_fast(mock_state, expr, [n], [(w, 0.5)])
        alpha_isr = evaluate_property_isr(state, expr, [n], [(w, 0.5)])
        np.testing.assert_allclose(alpha_isr, alpha_sos, atol=1e-8)


@pytest.mark.parametrize("ops", SOS_beta_like.keys())
class TestBetaLike:
    def test_h2o_sto3g_adc2(self, ops):
        case = "h2o_sto3g_adc2"
        if case not in cache.data_fulldiag:
            pytest.skip(f"{case} cache file not available.")
        molecule, basis, method = case.split("_")
        scfres = run_scf(molecule, basis)
        refstate = adcc.ReferenceState(scfres)
        expr = SOS_beta_like[ops]
        mock_state = cache.data_fulldiag[case]
        state = adcc.run_adc(refstate, method=method, n_singlets=5)

        omegas = [(w_o, 1), (w_2, 0.5)]
        beta_sos = evaluate_property_sos_fast(mock_state, expr, [n, k], omegas, extra_terms=False)
        beta_isr = evaluate_property_isr(state, expr, [n, k], omegas, extra_terms=False)
        np.testing.assert_allclose(beta_isr, beta_sos, atol=1e-8)


@pytest.mark.slow
@pytest.mark.parametrize("ops", SOS_gamma_like.keys())
class TestGammaLike:
    def test_h2o_sto3g_adc2(self, ops):
        case = "h2o_sto3g_adc2"
        if case not in cache.data_fulldiag:
            pytest.skip(f"{case} cache file not available.")
        molecule, basis, method = case.split("_")
        scfres = run_scf(molecule, basis)
        refstate = adcc.ReferenceState(scfres)
        expr = SOS_gamma_like[ops]
        mock_state = cache.data_fulldiag[case]
        state = adcc.run_adc(refstate, method=method, n_singlets=5)

        omegas = [(w_o, 1), (w_2, 0.5), (w_3, 0.5)]
        gamma_sos = evaluate_property_sos_fast(
            mock_state, expr, [n, m, p], omegas, extra_terms=False
        )
        gamma_isr = evaluate_property_isr(state, expr, [n, m, p], omegas, extra_terms=False)
        np.testing.assert_allclose(gamma_isr, gamma_sos, atol=1e-8)
