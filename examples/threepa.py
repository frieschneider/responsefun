from pyscf import gto, scf
import adcc
from responsefun.symbols_and_labels import (
    op_a, op_b, op_c,
    O, n, m, f, w_n, w_m, w_f,
    w_1, w_2, w_3
)
from responsefun.SumOverStates import TransitionMoment
from responsefun.evaluate_property import evaluate_property_isr
import numpy as np


def threepa_average(tens):
    assert np.shape(tens) == (3, 3, 3)
    return (1/35) * (2*np.einsum("abc,abc->", tens, tens) + 3*np.einsum("aab,bcc->", tens, tens))


# run SCF in PySCF
mol = gto.M(
    atom="""
    F       -0.000000   -0.000000    0.092567
    H        0.000000    0.000000   -0.833107
    """,
    unit="Angstrom",
    basis="augccpvdz"
)
scfres = scf.RHF(mol)
scfres.kernel()

# run ADC calculation using adcc
state = adcc.run_adc(scfres, method="adc2", n_singlets=10)
print(state.describe())

threepa_term = (
    TransitionMoment(O, op_a, n) * TransitionMoment(n, op_b, m)
    * TransitionMoment(m, op_c, f) / ((w_n - w_1) * (w_m - w_1 - w_2))
)

for es in range(5):
    omega_f = state.excitation_energy_uncorrected[es]
    print(f"===== State {es} ===== ")
    omegas = [(w_1, omega_f/3), (w_2, omega_f/3),
              (w_3, omega_f/3), (w_1, w_f-w_2-w_3)]
    threepa_tens = evaluate_property_isr(
        state, threepa_term, [n, m], omegas=omegas,
        perm_pairs=[(op_a, w_1), (op_b, w_2), (op_c, w_3)],
        conv_tol=1e-5, final_state=(f, es)
    )
    threepa_strength = threepa_average(threepa_tens)
    print(f"Transition strength (a.u.): {threepa_strength:.6f}")
