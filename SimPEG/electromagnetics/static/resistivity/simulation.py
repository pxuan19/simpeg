import numpy as np
import scipy as sp
import properties
from ....utils.code_utils import deprecate_class

from ....utils import mkvc, sdiag, Zero
from ....data import Data
from ...base import BaseEMSimulation
from .boundary_utils import getxBCyBC_CC
from .survey import Survey
from .fields import Fields3DCellCentered, Fields3DNodal
from .utils import _mini_pole_pole


class BaseDCSimulation(BaseEMSimulation):
    """
    Base DC Problem
    """

    survey = properties.Instance("a DC survey object", Survey, required=True)

    storeJ = properties.Bool("store the sensitivity matrix?", default=False)

    _mini_survey = None

    _mini_survey = None

    Ainv = None
    _Jmatrix = None
    gtgdiag = None

    def __init__(self, *args, **kwargs):
        miniaturize = kwargs.pop("miniaturize", False)
        super().__init__(*args, **kwargs)
        # Do stuff to simplify the forward and JTvec operation if number of dipole
        # sources is greater than the number of unique pole sources
        if miniaturize:
            self._dipoles, self._invs, self._mini_survey = _mini_pole_pole(self.survey)

    def fields(self, m=None, calcJ=True):
        if m is not None:
            self.model = m
            self._Jmatrix = None

        f = self.fieldsPair(self)
        if self.Ainv is not None:
            self.Ainv.clean()
        A = self.getA()
        self.Ainv = self.solver(A, **self.solver_opts)
        RHS = self.getRHS()

        f[:, self._solutionType] = self.Ainv * RHS

        return f

    def getJ(self, m, f=None):
        if self._Jmatrix is None:
            if f is None:
                f = self.fields(m)
            self._Jmatrix = self._Jtvec(m, v=None, f=f).T
        return self._Jmatrix

    def dpred(self, m=None, f=None):
        if self._mini_survey is not None:
            # Temporarily set self.survey to self._mini_survey
            survey = self.survey
            self.survey = self._mini_survey

        data = super().dpred(m=m, f=f)

        if self._mini_survey is not None:
            # reset survey
            self.survey = survey

        return self._mini_survey_data(data)

    def getJtJdiag(self, m, W=None):
        """
            Return the diagonal of JtJ
        """
        if self.gtgdiag is None:
            J = self.getJ(m)

            if W is None:
                W = np.ones(J.shape[0])
            else:
                W = W.diagonal() ** 2

            diag = np.zeros(J.shape[1])
            for i in range(J.shape[0]):
                diag += (W[i]) * (J[i] * J[i])

            self.gtgdiag = diag
        return self.gtgdiag

    def Jvec(self, m, v, f=None):
        """
            Compute sensitivity matrix (J) and vector (v) product.
        """

        if f is None:
            f = self.fields(m)

        if self.storeJ:
            J = self.getJ(m, f=f)
            return J.dot(v)

        self.model = m

        if self._mini_survey is not None:
            survey = self._mini_survey
        else:
            survey = self.survey

        Jv = []
        for source in survey.source_list:
            u_source = f[source, self._solutionType]  # solution vector
            dA_dm_v = self.getADeriv(u_source, v)
            dRHS_dm_v = self.getRHSDeriv(source, v)
            du_dm_v = self.Ainv * (-dA_dm_v + dRHS_dm_v)
            for rx in source.receiver_list:
                df_dmFun = getattr(f, "_{0!s}Deriv".format(rx.projField), None)
                df_dm_v = df_dmFun(source, du_dm_v, v, adjoint=False)
                Jv.append(rx.evalDeriv(source, self.mesh, f, df_dm_v))
        Jv = np.hstack(Jv)
        return self._mini_survey_data(Jv)

    def Jtvec(self, m, v, f=None):
        """
            Compute adjoint sensitivity matrix (J^T) and vector (v) product.
        """

        if f is None:
            f = self.fields(m)

        self.model = m

        if self.storeJ:
            J = self.getJ(m, f=f)
            return np.asarray(J.T.dot(v))

        return self._Jtvec(m, v=v, f=f)

    def _Jtvec(self, m, v=None, f=None):
        """
            Compute adjoint sensitivity matrix (J^T) and vector (v) product.
            Full J matrix can be computed by inputing v=None
        """

        if self._mini_survey is not None:
            survey = self._mini_survey
        else:
            survey = self.survey

        if v is not None:
            if isinstance(v, Data):
                v = v.dobs
            v = self._mini_survey_dataT(v)
            v = Data(survey, v)
            Jtv = np.zeros(m.size)
        else:
            # This is for forming full sensitivity matrix
            Jtv = np.zeros((self.model.size, survey.nD), order="F")
            istrt = int(0)
            iend = int(0)

        for source in survey.source_list:
            u_source = f[source, self._solutionType].copy()
            for rx in source.receiver_list:
                # wrt f, need possibility wrt m
                if v is not None:
                    PTv = rx.evalDeriv(
                        source, self.mesh, f, v[source, rx], adjoint=True
                    )
                else:
                    # This is for forming full sensitivity matrix
                    PTv = rx.getP(self.mesh, rx.projGLoc(f)).toarray().T
                df_duTFun = getattr(f, "_{0!s}Deriv".format(rx.projField), None)
                df_duT, df_dmT = df_duTFun(source, None, PTv, adjoint=True)

                ATinvdf_duT = self.Ainv * df_duT

                dA_dmT = self.getADeriv(u_source, ATinvdf_duT, adjoint=True)
                dRHS_dmT = self.getRHSDeriv(source, ATinvdf_duT, adjoint=True)
                du_dmT = -dA_dmT + dRHS_dmT
                if v is not None:
                    Jtv += (df_dmT + du_dmT).astype(float)
                else:
                    iend = istrt + rx.nD
                    if rx.nD == 1:
                        Jtv[:, istrt] = df_dmT + du_dmT
                    else:
                        Jtv[:, istrt:iend] = df_dmT + du_dmT
                    istrt += rx.nD

        if v is not None:
            return mkvc(Jtv)
        else:
            return (self._mini_survey_data(Jtv.T)).T

    def getSourceTerm(self):
        """
        Evaluates the sources, and puts them in matrix form
        :rtype: tuple
        :return: q (nC or nN, nSrc)
        """

        if self._mini_survey is not None:
            Srcs = self._mini_survey.source_list
        else:
            Srcs = self.survey.source_list

        if self._formulation == "EB":
            n = self.mesh.nN
            # return NotImplementedError

        elif self._formulation == "HJ":
            n = self.mesh.nC

        q = np.zeros((n, len(Srcs)), order="F")

        for i, source in enumerate(Srcs):
            q[:, i] = source.eval(self)
        return q

    @property
    def deleteTheseOnModelUpdate(self):
        toDelete = super(BaseDCSimulation, self).deleteTheseOnModelUpdate
        if self._Jmatrix is not None:
            toDelete += ["_Jmatrix"]
        if self.gtgdiag is not None:
            toDelete += ["gtgdiag"]
        return toDelete

    def _mini_survey_data(self, d_mini):
        if self._mini_survey is not None:
            out = d_mini[self._invs[0]]  # AM
            out[self._dipoles[0]] -= d_mini[self._invs[1]]  # AN
            out[self._dipoles[1]] -= d_mini[self._invs[2]]  # BM
            out[self._dipoles[0] & self._dipoles[1]] += d_mini[self._invs[3]]  # BN
        else:
            out = d_mini
        return out

    def _mini_survey_dataT(self, v):
        if self._mini_survey is not None:
            out = np.zeros(self._mini_survey.nD)
            # Need to use ufunc.at because there could be repeated indices
            # That need to be properly handled.
            np.add.at(out, self._invs[0], v)  # AM
            np.subtract.at(out, self._invs[1], v[self._dipoles[0]])  # AN
            np.subtract.at(out, self._invs[2], v[self._dipoles[1]])  # BM
            np.add.at(out, self._invs[3], v[self._dipoles[0] & self._dipoles[1]])  # BN
            return out
        else:
            out = v
        return out


class Simulation3DCellCentered(BaseDCSimulation):
    """
    3D cell centered DC problem
    """

    _solutionType = "phiSolution"
    _formulation = "HJ"  # CC potentials means J is on faces
    fieldsPair = Fields3DCellCentered
    bc_type = "Dirichlet"

    def __init__(self, mesh, **kwargs):

        BaseDCSimulation.__init__(self, mesh, **kwargs)
        self.setBC()

    def getA(self):
        """
        Make the A matrix for the cell centered DC resistivity problem
        A = D MfRhoI G
        """

        D = self.Div
        G = self.Grad
        MfRhoI = self.MfRhoI
        A = D @ MfRhoI @ G

        if self.bc_type == "Neumann":
            if self.verbose:
                print("Perturbing first row of A to remove nullspace for Neumann BC.")

            # Handling Null space of A
            I, J, V = sp.sparse.find(A[0, :])
            for jj in J:
                A[0, jj] = 0.0
            A[0, 0] = 1.0

        return A

    def getADeriv(self, u, v, adjoint=False):
        D = self.Div
        G = self.Grad
        MfRhoIDeriv = self.MfRhoIDeriv

        if adjoint:
            return MfRhoIDeriv(G @ u, D.T @ v, adjoint)

        return D * (MfRhoIDeriv(G @ u, v, adjoint))

    def getRHS(self):
        """
        RHS for the DC problem
        q
        """

        RHS = self.getSourceTerm()

        return RHS

    def getRHSDeriv(self, source, v, adjoint=False):
        """
        Derivative of the right hand side with respect to the model
        """
        # TODO: add qDeriv for RHS depending on m
        # qDeriv = source.evalDeriv(self, adjoint=adjoint)
        # return qDeriv
        return Zero()

    def setBC(self):
        if self.bc_type == "Dirichlet":
            if self.verbose:
                print(
                    "Homogeneous Dirichlet is the natural BC for this CC discretization."
                )
            self.Div = sdiag(self.mesh.vol) @ self.mesh.faceDiv
            self.Grad = self.Div.T

        else:
            if self.mesh._meshType == "TREE" and self.bc_type == "Neumann":
                raise NotImplementedError()

            if self.mesh.dim == 3:
                fxm, fxp, fym, fyp, fzm, fzp = self.mesh.faceBoundaryInd
                gBFxm = self.mesh.gridFx[fxm, :]
                gBFxp = self.mesh.gridFx[fxp, :]
                gBFym = self.mesh.gridFy[fym, :]
                gBFyp = self.mesh.gridFy[fyp, :]
                gBFzm = self.mesh.gridFz[fzm, :]
                gBFzp = self.mesh.gridFz[fzp, :]

                # Setup Mixed B.C (alpha, beta, gamma)
                temp_xm = np.ones_like(gBFxm[:, 0])
                temp_xp = np.ones_like(gBFxp[:, 0])
                temp_ym = np.ones_like(gBFym[:, 1])
                temp_yp = np.ones_like(gBFyp[:, 1])
                temp_zm = np.ones_like(gBFzm[:, 2])
                temp_zp = np.ones_like(gBFzp[:, 2])

                if self.bc_type == "Neumann":
                    if self.verbose:
                        print("Setting BC to Neumann.")
                    alpha_xm, alpha_xp = temp_xm * 0.0, temp_xp * 0.0
                    alpha_ym, alpha_yp = temp_ym * 0.0, temp_yp * 0.0
                    alpha_zm, alpha_zp = temp_zm * 0.0, temp_zp * 0.0

                    beta_xm, beta_xp = temp_xm, temp_xp
                    beta_ym, beta_yp = temp_ym, temp_yp
                    beta_zm, beta_zp = temp_zm, temp_zp

                    gamma_xm, gamma_xp = temp_xm * 0.0, temp_xp * 0.0
                    gamma_ym, gamma_yp = temp_ym * 0.0, temp_yp * 0.0
                    gamma_zm, gamma_zp = temp_zm * 0.0, temp_zp * 0.0

                elif self.bc_type == "Dirichlet":
                    if self.verbose:
                        print("Setting BC to Dirichlet.")
                    alpha_xm, alpha_xp = temp_xm, temp_xp
                    alpha_ym, alpha_yp = temp_ym, temp_yp
                    alpha_zm, alpha_zp = temp_zm, temp_zp

                    beta_xm, beta_xp = temp_xm * 0, temp_xp * 0
                    beta_ym, beta_yp = temp_ym * 0, temp_yp * 0
                    beta_zm, beta_zp = temp_zm * 0, temp_zp * 0

                    gamma_xm, gamma_xp = temp_xm * 0.0, temp_xp * 0.0
                    gamma_ym, gamma_yp = temp_ym * 0.0, temp_yp * 0.0
                    gamma_zm, gamma_zp = temp_zm * 0.0, temp_zp * 0.0

                elif self.bc_type == "Mixed":
                    # Ztop: Neumann
                    # Others: Mixed: alpha * phi + d phi dn = 0
                    # where alpha = 1 / r  * dr/dn
                    # (Dey and Morrison, 1979)

                    # This assumes that the source is located at
                    # (x_center, y_center_y, ztop)
                    # TODO: Implement Zhang et al. (1995)

                    xs = np.median(self.mesh.vectorCCx)
                    ys = np.median(self.mesh.vectorCCy)
                    zs = self.mesh.vectorCCz[-1]

                    def r_boundary(x, y, z):
                        return 1.0 / np.sqrt(
                            (x - xs) ** 2 + (y - ys) ** 2 + (z - zs) ** 2
                        )

                    rxm = r_boundary(gBFxm[:, 0], gBFxm[:, 1], gBFxm[:, 2])
                    rxp = r_boundary(gBFxp[:, 0], gBFxp[:, 1], gBFxp[:, 2])
                    rym = r_boundary(gBFym[:, 0], gBFym[:, 1], gBFym[:, 2])
                    ryp = r_boundary(gBFyp[:, 0], gBFyp[:, 1], gBFyp[:, 2])
                    rzm = r_boundary(gBFzm[:, 0], gBFzm[:, 1], gBFzm[:, 2])

                    alpha_xm = (gBFxm[:, 0] - xs) / rxm ** 2
                    alpha_xp = (gBFxp[:, 0] - xs) / rxp ** 2
                    alpha_ym = (gBFym[:, 1] - ys) / rym ** 2
                    alpha_yp = (gBFyp[:, 1] - ys) / ryp ** 2
                    alpha_zm = (gBFzm[:, 2] - zs) / rzm ** 2
                    alpha_zp = temp_zp.copy() * 0.0

                    beta_xm, beta_xp = temp_xm * 1, temp_xp * 1
                    beta_ym, beta_yp = temp_ym * 1, temp_yp * 1
                    beta_zm, beta_zp = temp_zm * 1, temp_zp * 1

                    gamma_xm, gamma_xp = temp_xm * 0.0, temp_xp * 0.0
                    gamma_ym, gamma_yp = temp_ym * 0.0, temp_yp * 0.0
                    gamma_zm, gamma_zp = temp_zm * 0.0, temp_zp * 0.0

                alpha = [alpha_xm, alpha_xp, alpha_ym, alpha_yp, alpha_zm, alpha_zp]
                beta = [beta_xm, beta_xp, beta_ym, beta_yp, beta_zm, beta_zp]
                gamma = [gamma_xm, gamma_xp, gamma_ym, gamma_yp, gamma_zm, gamma_zp]

            elif self.mesh.dim == 2:

                fxm, fxp, fym, fyp = self.mesh.faceBoundaryInd
                gBFxm = self.mesh.gridFx[fxm, :]
                gBFxp = self.mesh.gridFx[fxp, :]
                gBFym = self.mesh.gridFy[fym, :]
                gBFyp = self.mesh.gridFy[fyp, :]

                # Setup Mixed B.C (alpha, beta, gamma)
                temp_xm = np.ones_like(gBFxm[:, 0])
                temp_xp = np.ones_like(gBFxp[:, 0])
                temp_ym = np.ones_like(gBFym[:, 1])
                temp_yp = np.ones_like(gBFyp[:, 1])

                alpha_xm, alpha_xp = temp_xm * 0.0, temp_xp * 0.0
                alpha_ym, alpha_yp = temp_ym * 0.0, temp_yp * 0.0

                beta_xm, beta_xp = temp_xm, temp_xp
                beta_ym, beta_yp = temp_ym, temp_yp

                gamma_xm, gamma_xp = temp_xm * 0.0, temp_xp * 0.0
                gamma_ym, gamma_yp = temp_ym * 0.0, temp_yp * 0.0

                alpha = [alpha_xm, alpha_xp, alpha_ym, alpha_yp]
                beta = [beta_xm, beta_xp, beta_ym, beta_yp]
                gamma = [gamma_xm, gamma_xp, gamma_ym, gamma_yp]

            x_BC, y_BC = getxBCyBC_CC(self.mesh, alpha, beta, gamma)
            V = self.Vol
            self.Div = V * self.mesh.faceDiv
            P_BC, B = self.mesh.getBCProjWF_simple()
            M = B * self.mesh.aveCC2F
            self.Grad = self.Div.T - P_BC * sdiag(y_BC) * M


class Simulation3DNodal(BaseDCSimulation):
    """
    3D nodal DC problem
    """

    _solutionType = "phiSolution"
    _formulation = "EB"  # N potentials means B is on faces
    fieldsPair = Fields3DNodal

    def __init__(self, mesh, **kwargs):
        BaseDCSimulation.__init__(self, mesh, **kwargs)
        # Not sure why I need to do this
        # To evaluate mesh.aveE2CC, this is required....
        if mesh._meshType == "TREE":
            mesh.nodalGrad

    def getA(self):
        """
        Make the A matrix for the cell centered DC resistivity problem
        A = G.T MeSigma G
        """

        MeSigma = self.MeSigma
        Grad = self.mesh.nodalGrad
        A = Grad.T @ MeSigma @ Grad

        # Handling Null space of A
        I, J, V = sp.sparse.find(A[0, :])
        for jj in J:
            A[0, jj] = 0.0
        A[0, 0] = 1.0

        return A

    def getADeriv(self, u, v, adjoint=False):
        """
        Product of the derivative of our system matrix with respect to the
        model and a vector
        """
        Grad = self.mesh.nodalGrad
        if not adjoint:
            return Grad.T @ self.MeSigmaDeriv(Grad @ u, v, adjoint)
        elif adjoint:
            return self.MeSigmaDeriv(Grad @ u, Grad @ v, adjoint)

    def getRHS(self):
        """
        RHS for the DC problem
        q
        """

        RHS = self.getSourceTerm()
        return RHS

    def getRHSDeriv(self, source, v, adjoint=False):
        """
        Derivative of the right hand side with respect to the model
        """
        # TODO: add qDeriv for RHS depending on m
        # qDeriv = source.evalDeriv(self, adjoint=adjoint)
        # return qDeriv
        return Zero()


Simulation3DCellCentred = Simulation3DCellCentered  # UK and US!


############
# Deprecated
############


@deprecate_class(removal_version="0.15.0")
class Problem3D_N(Simulation3DNodal):
    pass


@deprecate_class(removal_version="0.15.0")
class Problem3D_CC(Simulation3DCellCentered):
    pass
