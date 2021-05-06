# Authors:
#     Loic Gouarin <loic.gouarin@cmap.polytechnique.fr>
#     Nicole Spillane <nicole.spillane@cmap.polytechnique.fr>
#
# License: BSD 3 clause
from .assembling import buildElasticityMatrix
from .bc import bcApplyWestMat, bcApplyWest_vec
from .cg import cg
from .projection import projection, GenEO_V0, minimal_V0, coarse_operators
from petsc4py import PETSc
from slepc4py import SLEPc
import mpi4py.MPI as mpi
import numpy as np
import scipy as sp
import copy

class PCBNN(object): #Neumann-Neumann and Additive Schwarz with no overlap
    def __init__(self, A_IS):
        """
        Initialize the domain decomposition preconditioner, multipreconditioner and coarse space with its operators

        Parameters
        ==========

        A_IS : petsc.Mat
            The matrix of the problem in IS format. A must be a symmetric positive definite matrix
            with symmetric positive semi-definite submatrices

        PETSc.Options
        =============

        PCBNN_switchtoASM :Bool
            Default is False
            If True then the domain decomposition preconditioner is the BNN preconditioner. If false then the domain
            decomposition precondition is the Additive Schwarz preconditioner with minimal overlap.

        PCBNN_kscaling : Bool
            Default is True.
            If true then kscaling (partition of unity that is proportional to the diagonal of the submatrices of A)
            is used when a partition of unity is required. Otherwise multiplicity scaling is used when a partition
            of unity is required. This may occur in two occasions:
              - to scale the local BNN matrices if PCBNN_switchtoASM=True,
              - in the GenEO eigenvalue problem for eigmin if PCBNN_switchtoASM=False and PCBNN_GenEO=True with
                PCBNN_GenEO_eigmin > 0 (see projection.__init__ for the meaning of these options).

        PCBNN_verbose : Bool
            If True, some information about the preconditioners is printed when the code is executed.

        PCBNN_GenEO : Bool
            Default is False.
            If True then the coarse space is enriched by solving local generalized eigenvalue problems.

        PCBNN_CoarseProjection : Bool
            Default is True.
            If False then there is no coarse projection: Two level Additive Schwarz or One-level preconditioner depending on PCBNN_addCoarseSolve.
            If True, the coarse projection is applied: Projected preconditioner of hybrid preconditioner depending on PCBNN_addCoarseSolve.

        PCBNN_addCoarseSolve : Bool
            Default is False.
            If True then (R0t A0\R0 r) is added to the preconditioned residual.
            False corresponds to the projected preconditioner (need to choose initial guess accordingly) (or the one level preconditioner if PCBNN_CoarseProjection = False).
            True corresponds to the hybrid preconditioner (or the fully additive preconditioner if PCBNN_CoarseProjection = False).
        """
        OptDB = PETSc.Options()
        self.switchtoASM = OptDB.getBool('PCBNN_switchtoASM', False) #use Additive Schwarz as a preconditioner instead of BNN
        self.kscaling = OptDB.getBool('PCBNN_kscaling', True) #kscaling if true, multiplicity scaling if false
        self.verbose = OptDB.getBool('PCBNN_verbose', False)
        self.GenEO = OptDB.getBool('PCBNN_GenEO', True)
        self.addCS = OptDB.getBool('PCBNN_addCoarseSolve', False)
        self.projCS = OptDB.getBool('PCBNN_CoarseProjection', True)

        #extract Neumann matrix from A in IS format
        Ms = A_IS.copy().getISLocalMat()

        # convert A_IS from matis to mpiaij
        A_mpiaij = A_IS.convert('mpiaij')
        r, _ = A_mpiaij.getLGMap() #r, _ = A_IS.getLGMap()
        is_A = PETSc.IS().createGeneral(r.indices)
        # extract exact local solver
        As = A_mpiaij.createSubMatrices(is_A)[0]

        vglobal, _ = A_mpiaij.getVecs()
        vlocal, _ = Ms.getVecs()
        scatter_l2g = PETSc.Scatter().create(vlocal, None, vglobal, is_A)

        #compute the multiplicity of each degree
        vlocal.set(1.)
        vglobal.set(0.)
        scatter_l2g(vlocal, vglobal, PETSc.InsertMode.ADD_VALUES)
        scatter_l2g(vglobal, vlocal, PETSc.InsertMode.INSERT_VALUES, PETSc.ScatterMode.SCATTER_REVERSE)
        NULL,mult_max = vglobal.max()

        # k-scaling or multiplicity scaling of the local (non-assembled) matrix
        if self.kscaling == False:
            Ms.diagonalScale(vlocal,vlocal)
        else:
            v1 = As.getDiagonal()
            v2 = Ms.getDiagonal()
            Ms.diagonalScale(v1/v2, v1/v2)

        # the default local solver is the scaled non assembled local matrix (as in BNN)
        if self.switchtoASM:
            Atildes = As
            if mpi.COMM_WORLD.rank == 0:
                print('The user has chosen to switch to Additive Schwarz instead of BNN.')
        else: #(default)
            Atildes = Ms
        ksp_Atildes = PETSc.KSP().create(comm=PETSc.COMM_SELF)
        ksp_Atildes.setOptionsPrefix("ksp_Atildes_")
        ksp_Atildes.setOperators(Atildes)
        ksp_Atildes.setType('preonly')
        pc_Atildes = ksp_Atildes.getPC()
        pc_Atildes.setType('cholesky')
        pc_Atildes.setFactorSolverType('mumps')
        ksp_Atildes.setFromOptions()

        ksp_Atildes_forSLEPc = PETSc.KSP().create(comm=PETSc.COMM_SELF)
        ksp_Atildes_forSLEPc.setOptionsPrefix("ksp_Atildes_")
        ksp_Atildes_forSLEPc.setOperators(Atildes)
        ksp_Atildes_forSLEPc.setType('preonly')
        pc_Atildes_forSLEPc = ksp_Atildes_forSLEPc.getPC()
        pc_Atildes_forSLEPc.setType('cholesky')
        pc_Atildes_forSLEPc.setFactorSolverType('mumps')
        ksp_Atildes_forSLEPc.setFromOptions()

        self.A = A_mpiaij
        self.Ms = Ms
        self.As = As
        self.ksp_Atildes = ksp_Atildes
        self.ksp_Atildes_forSLEPc = ksp_Atildes_forSLEPc
        self.work = vglobal.copy()
        self.works_1 = vlocal.copy()
        self.works_2 = self.works_1.copy()
        self.scatter_l2g = scatter_l2g
        self.mult_max = mult_max

        self.minV0 = minimal_V0(self.ksp_Atildes)
        if self.GenEO == True:
          GenEOV0 = GenEO_V0(self.ksp_Atildes_forSLEPc,self.Ms,self.As,self.mult_max,self.minV0.V0s)
          self.V0s = GenEOV0.V0s
        else:
          self.V0s = self.minV0.V0s
        self.proj = coarse_operators(self.V0s,self.A,self.scatter_l2g,vlocal,self.work)

        #self.proj = projection(self)

    def mult(self, x, y):
        """
        Applies the domain decomposition preconditioner followed by the projection preconditioner to a vector.

        Parameters
        ==========

        x : petsc.Vec
            The vector to which the preconditioner is to be applied.

        y : petsc.Vec
            The vector that stores the result of the preconditioning operation.

        """
########################
########################
        xd = x.copy()
        if self.projCS == True:
            self.proj.project_transpose(xd)

        self.scatter_l2g(xd, self.works_1, PETSc.InsertMode.INSERT_VALUES, PETSc.ScatterMode.SCATTER_REVERSE)
        self.ksp_Atildes.solve(self.works_1, self.works_2)

        y.set(0.)
        self.scatter_l2g(self.works_2, y, PETSc.InsertMode.ADD_VALUES)
        if self.projCS == True:
            self.proj.project(y)

        if self.addCS == True:
            xd = x.copy()
            ytild = self.proj.coarse_init(xd) # I could save a coarse solve by combining this line with project_transpose
            y += ytild

    def MP_mult(self, x, y):
        """
        Applies the domain decomposition multipreconditioner followed by the projection preconditioner to a vector.

        Parameters
        ==========

        x : petsc.Vec
            The vector to which the preconditioner is to be applied.

        y : FIX
            The list of ndom vectors that stores the result of the multipreconditioning operation (one vector per subdomain).

        """
        self.scatter_l2g(x, self.works_1, PETSc.InsertMode.INSERT_VALUES, PETSc.ScatterMode.SCATTER_REVERSE)
        self.ksp_Atildes.solve(self.works_1, self.works_2)
        for i in range(mpi.COMM_WORLD.size):
            self.works_1.set(0)
            if mpi.COMM_WORLD.rank == i:
                self.works_1 = self.works_2.copy()
            y[i].set(0.)
            self.scatter_l2g(self.works_1, y[i], PETSc.InsertMode.ADD_VALUES)
            self.proj.project(y[i])

    def apply(self,pc, x, y):
        """
        Applies the domain decomposition preconditioner followed by the projection preconditioner to a vector.
        This is just a call to PCBNN.mult with the function name and arguments that allow PCBNN to be passed
        as a preconditioner to PETSc.ksp.

        Parameters
        ==========

        pc: This argument is not called within the function but it belongs to the standard way of calling a preconditioner.

        x : petsc.Vec
            The vector to which the preconditioner is to be applied.

        y : petsc.Vec
            The vector that stores the result of the preconditioning operation.

        """
        self.mult(x,y)

class PCNew:
    def __init__(self, A_IS):
        OptDB = PETSc.Options()
        self.switchtoASM = OptDB.getBool('PCNew_switchtoASM', False) #use Additive Schwarz as a preconditioner instead of BNN
        self.verbose = OptDB.getBool('PCNew_verbose', False)
        self.GenEO = OptDB.getBool('PCNew_GenEO', True)
        self.addCS = OptDB.getBool('PCNew_addCoarseSolve', False)
        self.projCS = OptDB.getBool('PCNew_CoarseProjection', True)
        self.nev = OptDB.getInt('PCNew_Bs_nev', 20) #number of vectors asked to SLEPc for cmputing negative part of Bs

        # Compute Bs (the symmetric matrix in the algebraic splitting of A)
        # TODO: implement without A in IS format
        ANeus = A_IS.getISLocalMat() #only the IS is used for the algorithm,
        Mu = A_IS.copy()
        Mus = Mu.getISLocalMat() #the IS format is used to compute Mu (multiplicity of each pair of dofs)

        for i in range(ANeus.getSize()[0]):
            col, _ = ANeus.getRow(i)
            Mus.setValues([i], col, np.ones_like(col))
        Mu.restoreISLocalMat(Mus)
        Mu.assemble()
        Mu = Mu.convert('mpiaij')

        A_mpiaij = A_IS.convert('mpiaij')
        B = A_mpiaij.duplicate()
        for i in range(*A_mpiaij.getOwnershipRange()):
            a_cols, a_values = A_mpiaij.getRow(i)
            _, b_values = Mu.getRow(i)
            B.setValues([i], a_cols, a_values/b_values, PETSc.InsertMode.INSERT_VALUES)

        B.assemble()

        # B.view()
        # A_mpiaij.view()
        # (A_mpiaij - B).view()
        # data = ANeus.getArray()
        # if mpi.COMM_WORLD.rank == 0:
        #     print(dir(ANeus))
        #     print(type(ANeus), ANeus.getType())
###################@


        # convert A_IS from matis to mpiaij
        #A_mpiaij = A_IS.convertISToAIJ()
        r, _ = A_mpiaij.getLGMap() #r, _ = A_IS.getLGMap()
        is_A = PETSc.IS().createGeneral(r.indices)
        # extract exact local solver
        As = A_mpiaij.createSubMatrices(is_A)[0]
        Bs = B.createSubMatrices(is_A)[0]

        #mumps solver for Bs
        Bs_ksp = PETSc.KSP().create(comm=PETSc.COMM_SELF)
        Bs_ksp.setOptionsPrefix("Bs_ksp_")
        Bs_ksp.setOperators(Bs)
        Bs_ksp.setType('preonly')
        Bs_pc = Bs_ksp.getPC()
        Bs_pc.setType('cholesky')
        Bs_pc.setFactorSolverType('mumps')
        Bs_pc.setFactorSetUpSolverType()
        Bs_pc.setUp()
        Bs_ksp.setFromOptions()


        #temp = Bs.getValuesCSR()

        work, _ = A_mpiaij.getVecs()
        works, _ = As.getVecs()
        works_2 = works.duplicate()
        mus = works.duplicate()
        scatter_l2g = PETSc.Scatter().create(works, None, work, is_A)

        #compute the multiplicity of each dof
        work = Mu.getDiagonal()
        NULL,mult_max = work.max()

        scatter_l2g(work, mus, PETSc.InsertMode.INSERT_VALUES, PETSc.ScatterMode.SCATTER_REVERSE)
        invmus = mus.duplicate()
        invmus = 1/mus
        #if mpi.COMM_WORLD.rank == 0:
        #    invmus.view()
        #print(f'multmax: {mult_max}')


        DVnegs = []
        Vnegs = []
        invmusVnegs = []

        #BEGIN diagonalize Bs
        #Eigenvalue Problem for smallest eigenvalues
        eps = SLEPc.EPS().create(comm=PETSc.COMM_SELF)
        eps.setDimensions(nev=self.nev)
        eps.setProblemType(SLEPc.EPS.ProblemType.HEP)
        eps.setOperators(Bs)

        #print(f'dimension of Bs : {Bs.getSize()}')


        #OPTION 1: works but dense algebra
        eps.setType(SLEPc.EPS.Type.LAPACK)
        eps.setWhichEigenpairs(SLEPc.EPS.Which.SMALLEST_REAL) #with lapack this just tells slepc how to order the eigenpairs
        ##END OPTION 1

        ##OPTION 2: default solver (Krylov Schur) but error with getInertia - is there a MUMPS mattype - Need to use MatCholeskyFactor
               #if Which eigenpairs is set to SMALLEST_REAL, some are computed but not all

        ##Bs.setOption(PETSc.Mat.Option.SYMMETRIC, True)
        ##Bs.convert('sbaij')
        ##IScholBs = is_A.duplicate()
        ##Bs.factorCholesky(IScholBs) #not implemented
        #tempksp = PETSc.KSP().create(comm=PETSc.COMM_SELF)
        #tempksp.setOperators(Bs)
        #tempksp.setType('preonly')
        #temppc = tempksp.getPC()
        #temppc.setType('cholesky')
        #temppc.setFactorSolverType('mumps')
        #temppc.setFactorSetUpSolverType()
        #tempF = temppc.getFactorMatrix()
        #tempF.setMumpsIcntl(13, 1) #needed to compute intertia according to slepcdoc, inertia computation still doesn't work though
        #temppc.setUp()
        ##eps.setOperators(tempF)
        #eps.setWhichEigenpairs(SLEPc.EPS.Which.ALL)
        #eps.setInterval(PETSc.NINFINITY,0.0)
        #eps.setUp()

        ##eps.setWhichEigenpairs(SLEPc.EPS.Which.TARGET_REAL)
        ##eps.setTarget(0.)

        ##if len(Vnegs) > 0 :
        ##    eps.setDeflationSpace(Vnegs)
        ##if mpi.COMM_WORLD.rank == 0:
        ##    eps.view()
        ##END OPTION 2

        eps.solve()
        if eps.getConverged() < self.nev:
            PETSc.Sys.Print('for Bs in subdomain {}: {} eigenvalues converged (less that the {} requested)'.format(mpi.COMM_WORLD.rank, eps.getConverged(), self.nev), comm=PETSc.COMM_SELF)

        Dnegs = []
        Dposs = []
        for i in range(eps.getConverged()):
            tempscalar = np.real(eps.getEigenvalue(i))
            if tempscalar < 0. :
                Dnegs.append(-1.*tempscalar)
                Vnegs.append(works.duplicate())
                eps.getEigenvector(i,Vnegs[-1])
                DVnegs.append(Dnegs[-1] * Vnegs[-1])
                invmusVnegs.append(invmus * Vnegs[-1])
            else :
                Dposs.append(tempscalar)
        PETSc.Sys.Print('for Bs in subdomain {}: ncv= {} with {} negative eigs (nev = {})'.format(mpi.COMM_WORLD.rank, eps.getConverged(), len(Vnegs), self.nev), comm=PETSc.COMM_SELF)
        #PETSc.Sys.Print('for Bs in subdomain {}, eigenvalues: {} {}'.format(mpi.COMM_WORLD.rank, Dnegs, Dposs), comm=PETSc.COMM_SELF)
        nnegs = len(Dnegs)
        #print(f'length of Dnegs {nnegs}')
        #print(f'values of Dnegs {np.array(Dnegs)}')


#        Dnegs = np.diag(Dnegs) #TODO Loic: make it sparse
#        Vnegs = np.array(Vnegs)
#        DVnegs = Dnegs.dot(Vnegs)
#        Dposs = np.diag(Dposs)
#        print(f' diag of Dnegs {np.diag(Dnegs)}')
#        print(f'shape of Dnegs {Dnegs.shape}')
#        print(f'shape of Vnegs {Vnegs.shape}')
        #END diagonalize Bs

#        self.Vnegs = Vnegs
#        self.DVnegs = DVnegs
#        self.scatterl

#Local Apos and Aneg
        Aneg = PETSc.Mat().createPython([work.getSizes(), work.getSizes()], comm=PETSc.COMM_WORLD)
        Aneg.setPythonContext(Aneg_ctx(Vnegs, DVnegs, scatter_l2g, works, work))
        Aneg.setUp()

        Apos = PETSc.Mat().createPython([work.getSizes(), work.getSizes()], comm=PETSc.COMM_WORLD)
        Apos.setPythonContext(Apos_ctx(A_mpiaij, Aneg ))
        Apos.setUp()
        #A pos = A_mpiaij + Aneg so it could be a composite matrix rather than Python type

        Anegs = PETSc.Mat().createPython([works.getSizes(), works.getSizes()], comm=PETSc.COMM_SELF)
        Anegs.setPythonContext(Anegs_ctx(Vnegs, DVnegs))
        Anegs.setUp()

        Aposs = PETSc.Mat().createPython([works.getSizes(), works.getSizes()], comm=PETSc.COMM_SELF)
        Aposs.setPythonContext(Aposs_ctx(Bs, Anegs ))
        Aposs.setUp()

        projVnegs = PETSc.Mat().createPython([works.getSizes(), works.getSizes()], comm=PETSc.COMM_SELF)
        projVnegs.setPythonContext(projVnegs_ctx(Vnegs))
        projVnegs.setUp()

        projVposs = PETSc.Mat().createPython([works.getSizes(), works.getSizes()], comm=PETSc.COMM_SELF)
        projVposs.setPythonContext(projVposs_ctx(projVnegs))
        projVposs.setUp()

        #TODO Implement RsAposRsts, this is the restriction of Apos to the dofs in this subdomain. So it applies to local vectors but has non local operations
        #RsAposRsts = PETSc.Mat().createPython([works.getSizes(), works.getSizes()], comm=PETSc.COMM_SELF) #or COMM_WORLD ?
        #RsAposRsts.setPythonContext(RsAposRsts_ctx(s,Apos,scatter_l2g))
        #RsAposRsts.setUp()

        invAposs = PETSc.Mat().createPython([works.getSizes(), works.getSizes()], comm=PETSc.COMM_SELF)
        invAposs.setPythonContext(invAposs_ctx(Bs_ksp, projVposs ))
        invAposs.setUp()

        ksp_Aposs = PETSc.KSP().create(comm=PETSc.COMM_SELF)
        ksp_Aposs.setOperators(Aposs)
        ksp_Aposs.setType('preonly')
        pc_Aposs = ksp_Aposs.getPC()
        pc_Aposs.setType('python')
        pc_Aposs.setPythonContext(invAposs_ctx(Bs_ksp,projVposs))
        ksp_Aposs.setUp()
        work.set(1.)

        Ms = PETSc.Mat().createPython([works.getSizes(), works.getSizes()], comm=PETSc.COMM_SELF)
        Ms.setPythonContext(scaledmats_ctx(Aposs, mus, mus))
        Ms.setUp()

        ksp_Ms = PETSc.KSP().create(comm=PETSc.COMM_SELF)
        ksp_Ms.setOptionsPrefix("ksp_Ms_")
        ksp_Ms.setOperators(Ms)
        ksp_Ms.setType('preonly')
        pc_Ms = ksp_Ms.getPC()
        pc_Ms.setType('python')
        pc_Ms.setPythonContext(scaledmats_ctx(invAposs,invmus,invmus) )
        ksp_Ms.setFromOptions()

        ksp_Ms_forSLEPc = PETSc.KSP().create(comm=PETSc.COMM_SELF)
        ksp_Ms_forSLEPc.setOptionsPrefix("ksp_Ms_")
        ksp_Ms_forSLEPc.setOperators(Ms)
        ksp_Ms_forSLEPc.setType('preonly')
        pc_Ms_forSLEPc = ksp_Ms_forSLEPc.getPC()
        pc_Ms_forSLEPc.setType('python')
        pc_Ms_forSLEPc.setPythonContext(scaledmats_ctx(invAposs,invmus,invmus) )
        ksp_Ms_forSLEPc.setFromOptions()

        # the default local solver is the scaled non assembled local matrix (as in BNN)
        if self.switchtoASM:
            print('The GenEO coarse space for this Atildes with global opeator Apos is not implemented yet, the component for eigmax is missing')
            Atildes = As
            if mpi.COMM_WORLD.rank == 0:
                print('The user has chosen to switch to Additive Schwarz instead of BNN.')
            ksp_Atildes = PETSc.KSP().create(comm=PETSc.COMM_SELF)
            ksp_Atildes.setOptionsPrefix("ksp_Atildes_")
            ksp_Atildes.setOperators(Atildes)
            ksp_Atildes.setType('preonly')
            pc_Atildes = ksp_Atildes.getPC()
            pc_Atildes.setType('cholesky')
            pc_Atildes.setFactorSolverType('mumps')
            ksp_Atildes.setFromOptions()
            minV0s = minimal_V0(ksp_Atildes).V0s

            ksp_Atildes_forSLEPc = PETSc.KSP().create(comm=PETSc.COMM_SELF)
            ksp_Atildes_forSLEPc.setOptionsPrefix("ksp_Atildes_")
            ksp_Atildes_forSLEPc.setOperators(Atildes)
            ksp_Atildes_forSLEPc.setType('preonly')
            pc_Atildes_forSLEPc = ksp_Atildes_forSLEPc.getPC()
            pc_Atildes_forSLEPc.setType('cholesky')
            pc_Atildes_forSLEPc.setFactorSolverType('mumps')
            ksp_Atildes_forSLEPc.setFromOptions()
        else: #(default)
            Atildes = Ms
            ksp_Atildes = PETSc.KSP().create(comm=PETSc.COMM_SELF)
            ksp_Atildes.setOptionsPrefix("ksp_Atildes_")
            ksp_Atildes.setOperators(Atildes)
            ksp_Atildes.setType('preonly')
            pc_Atildes = ksp_Atildes.getPC()
            pc_Atildes.setType('python')
            pc_Atildes.setPythonContext(scaledmats_ctx(invAposs,invmus,invmus) )
            ksp_Atildes.setFromOptions()
            minV0s = invmusVnegs

            ksp_Atildes_forSLEPc = PETSc.KSP().create(comm=PETSc.COMM_SELF)
            ksp_Atildes_forSLEPc.setOptionsPrefix("ksp_Atildes_")
            ksp_Atildes_forSLEPc.setOperators(Atildes)
            ksp_Atildes_forSLEPc.setType('preonly')
            pc_Atildes_forSLEPc = ksp_Atildes_forSLEPc.getPC()
            pc_Atildes_forSLEPc.setType('python')
            pc_Atildes_forSLEPc.setPythonContext(scaledmats_ctx(invAposs,invmus,invmus) )
            ksp_Atildes_forSLEPc.setFromOptions()

        self.A = A_mpiaij
        self.Apos = Apos
        self.Ms = Ms
        self.As = As
        self.ksp_Atildes = ksp_Atildes
        self.ksp_Ms = ksp_Ms
        self.ksp_Atildes_forSLEPc = ksp_Atildes_forSLEPc
        self.ksp_Ms_forSLEPc = ksp_Ms_forSLEPc
        self.work = work
        self.works_1 = works
        self.works_2 = works_2
        self.scatter_l2g = scatter_l2g
        self.mult_max = mult_max
        self.ksp_Atildes = ksp_Atildes

        if self.GenEO == True:
          GenEOV0 = GenEO_V0(self.ksp_Atildes_forSLEPc,self.Ms,self.As,self.mult_max,minV0s,self.ksp_Ms_forSLEPc)
          self.V0s = GenEOV0.V0s
        else:
          self.V0s = minV0s

        self.proj = coarse_operators(self.V0s,self.Apos,self.scatter_l2g,self.works_1,self.work)

##Debug DEBUG
        works_3 = works.copy()
##projVnegs is a projection
#        #works.setRandom()
#        works.set(1.)
#        projVnegs.mult(works,works_2)
#        projVnegs.mult(works_2,works_3)
#        print(f'check that projVnegs is a projection {works_2.norm()} = {works_3.norm()} < {works.norm()}')
##projVposs is a projection
##Pythagoras ok
#        works.setRandom()
#        #works.set(1.)
#        projVnegs.mult(works,works_2)
#        projVposs.mult(works,works_3)
#        print(f'{works_2.norm()**2} +  {works_3.norm()**2}= {works_2.norm()**2 +  works_3.norm()**2}  =  {(works.norm())**2}') 
#        print(f'0 = {(works - works_2 - works_3).norm()} if the two projections sum to identity')
##Aposs = projVposs Bs projVposs = Bs projVposs  (it is implemented as Bs + Anegs)
#        works_4 = works.copy()
#        works.setRandom()
#        #works.set(1.)
#        projVposs.mult(works,works_2)
#        Bs.mult(works_2,works_3)
#        projVposs.mult(works_3,works_2)
#        Aposs.mult(works,works_4)
#        print(f'check Aposs = projVposs Bs projVposs = Bs projVposs: {works_2.norm()} = {works_3.norm()} = {works_4.norm()}')
#        print(f'norms of diffs (should be zero): {(works_2 - works_3).norm()}, {(works_2 - works_4).norm()}, {(works_3 - works_4).norm()}')
###check that Aposs > 0 and Anegs >0 but Bs is indefinite + "Pythagoras"  
#        works_4 = works.copy()
#        works.set(1.) #(with vector full of ones I get a negative Bs semi-norm) 
#        Bs.mult(works,works_4)
#        Aposs.mult(works,works_2)
#        Anegs.mult(works,works_3)
#        print(f'|.|_Bs {works_4.dot(works)} (can be neg or pos); |.|_Aposs {works_2.dot(works)} > 0;  |.|_Anegs  {works_3.dot(works)} >0')
#        print(f' |.|_Bs^2 = |.|_Aposs^2 -  |.|_Anegs ^2 = {works_2.dot(works)} - {works_3.dot(works)} = {works_2.dot(works) - works_3.dot(works)} = {works_4.dot(works)} ')##
###check that ksp_Aposs.solve(Aposs *  x) = projVposs x         
#        works_4 = works.copy()
#        works.setRandom()
#        #works.set(1.)
#        projVposs.mult(works,works_2)
#        Aposs(works,works_3)
#        ksp_Aposs.solve(works_3,works_4)  
#        works_5 = works_2 - works_4 
#        print(f'norm x = {works.norm()}; norm projVposs x = {works_2.norm()} = norm Aposs\Aposs*x = {works_4.norm()}; normdiff = {works_5.norm()}')
####check that mus*invmus = vec of ones
#        works.set(1.0)
#        works_2 = invmus*mus
#        works_3 = works - works_2
#        print(f'0 = norm(vec of ones - mus*invmus)   = {works_3.norm()}, mus in [{mus.min()}, {mus.max()}], invmus in [{invmus.min()}, {invmus.max()}]')
###check that Ms*ksp_Ms.solve(Ms*x) = Ms*x  
#        works_4 = works.copy()
#        works.setRandom()
#        Atildes.mult(works,works_3)
#        self.ksp_Atildes.solve(works_3,works_4)  
#        #HERE
#        Atildes.mult(works_4,works_2)
#        works_5 = works_2 - works_3 
#        print(f'norm x = {works.norm()}; Atilde*x = {works_3.norm()} = norm Atilde*(Atildes\Atildes)*x = {works_2.norm()}; normdiff = {works_5.norm()}')
###check Apos by implementing it a different way in Apos_debug
#        Apos_debug = PETSc.Mat().createPython([work.getSizes(), work.getSizes()], comm=PETSc.COMM_WORLD)
#        Apos_debug.setPythonContext(Apos_debug_ctx(projVposs, Aposs, scatter_l2g, works, work))
#        Apos_debug.setUp()
#        work.setRandom()
#        test = work.duplicate()
#        test2 = work.duplicate()
#        Apos.mult(work,test)
#        Apos_debug.mult(work,test2)
#        testdiff = test-test2
#        print(f'norm of |.|_Apos = {np.sqrt(test.dot(work))} = |.|_Apos_debug = {np.sqrt(test2.dot(work))} ; norm of diff = {testdiff.norm()}')
### 
###check that the projection in proj is a self.proj.A orth projection
#        #work.setRandom()
#        work.set(1.)
#        test = work.copy()
#        self.proj.project(test)
#        test2 = test.copy()
#        self.proj.project(test2)
#        testdiff = test-test2
#        print(f'norm(Pi x - Pi Pix) = {testdiff.norm()} = 0') 
#        self.proj.A.mult(test,test2)
#        test3 = work.duplicate()
#        self.proj.A.mult(work,test3)
#        print(f'|Pi x|_A^2 - |x|_A^2 = {test.dot(test2)} - {work.dot(test3)} = {test.dot(test2) - work.dot(test3)} < 0 ')
#        #test2 = A Pi x ( = Pit A Pi x)
#        test3 = test2.copy()
#        self.proj.project_transpose(test3)
#        test = test3.copy()
#        self.proj.project_transpose(test)
#        testdiff = test3 - test2
#        print(f'norm(A Pi x - Pit A Pix) = {testdiff.norm()} = 0 = {(test - test3).norm()} = norm(Pit Pit A Pi x - Pit A Pix); compare with norm(A Pi x) = {test2.norm()} ') 
#        #work.setRandom()
#        work.set(1.)
#        test2 = work.copy()
#        self.proj.project_transpose(test2)
#        test2 = -1*test2
#        test2 += work
#    
#        test = work.copy()
#        test = self.proj.coarse_init(work)
#        test3 = work.duplicate()
#        self.proj.A.mult(test,test3)
#
#        print(f'norm(A coarse_init(b)) = {test3.norm()} = {test2.norm()} = norm((I-Pit b)); norm diff = {(test2 - test3).norm()}')   
#
## END Debug DEBUG
#        self.work.setRandom()
#        test = work.duplicate()
#        self.apply([],self.work,test)
#        print('self.apply applied')
#        test.view()
#TODO: + define a new class for preconditioner of Apos to keep the apply in this class for the preconditioner for A
#      + compute R0t = Apos \ Rst Vnegs for every s and every vector in Vnegs
#      + define coarse operators for A with R0t
#      + define global preconditioner for A
 
    def mult(self, x, y):
        """
        Applies the domain decomposition preconditioner followed by the projection preconditioner to a vector.

        Parameters
        ==========

        x : petsc.Vec
            The vector to which the preconditioner is to be applied.

        y : petsc.Vec
            The vector that stores the result of the preconditioning operation.

        """
########################
########################
        xd = x.copy()
        if self.projCS == True:
            self.proj.project_transpose(xd)

        self.scatter_l2g(xd, self.works_1, PETSc.InsertMode.INSERT_VALUES, PETSc.ScatterMode.SCATTER_REVERSE)
        self.ksp_Atildes.solve(self.works_1, self.works_2)

        y.set(0.)
        self.scatter_l2g(self.works_2, y, PETSc.InsertMode.ADD_VALUES)
        if self.projCS == True:
            self.proj.project(y)

        if self.addCS == True:
            xd = x.copy()
            ytild = self.proj.coarse_init(xd) # I could save a coarse solve by combining this line with project_transpose
            y += ytild

    def MP_mult(self, x, y):
        """
        Applies the domain decomposition multipreconditioner followed by the projection preconditioner to a vector.

        Parameters
        ==========

        x : petsc.Vec
            The vector to which the preconditioner is to be applied.

        y : FIX
            The list of ndom vectors that stores the result of the multipreconditioning operation (one vector per subdomain).

        """
        print('not implemented')

    def apply(self, pc, x, y):
        """
        Applies the domain decomposition preconditioner followed by the projection preconditioner to a vector.
        This is just a call to PCNew.mult with the function name and arguments that allow PCNew to be passed
        as a preconditioner to PETSc.ksp.

        Parameters
        ==========

        pc: This argument is not called within the function but it belongs to the standard way of calling a preconditioner.

        x : petsc.Vec
            The vector to which the preconditioner is to be applied.

        y : petsc.Vec
            The vector that stores the result of the preconditioning operation.

        """
        self.mult(x,y)

class Aneg_ctx(object):
    def __init__(self, Vnegs, DVnegs, scatter_l2g, works, work):
        self.scatter_l2g = scatter_l2g
        self.work = works
        self.works = works
        self.Vnegs = Vnegs
        self.DVnegs = DVnegs
        self.gamma = PETSc.Vec().create(comm=PETSc.COMM_SELF)
        self.gamma.setType(PETSc.Vec.Type.SEQ)
        self.gamma.setSizes(len(self.Vnegs))
    def mult(self, mat, x, y):
        y.set(0)
        self.scatter_l2g(x, self.works, PETSc.InsertMode.INSERT_VALUES, PETSc.ScatterMode.SCATTER_REVERSE)
        for i,vec in enumerate(self.DVnegs):
            self.gamma[i] = self.works.dot(vec)
        self.works.set(0)
        for i,vec in enumerate(self.Vnegs):
            #if mpi.COMM_WORLD.rank == 0:
            #    print(f'self.gamma[i]: {self.gamma[i]}')
            self.works.axpy(self.gamma[i], vec)
        self.scatter_l2g(self.works, y, PETSc.InsertMode.ADD_VALUES)

class Apos_debug_ctx(object):
    def __init__(self, projVposs, Aposs, scatter_l2g, works, work):
        self.scatter_l2g = scatter_l2g
        self.work = works
        self.works = works
        self.projVposs = projVposs
        self.Aposs = Aposs
    def mult(self, mat, x, y):
        y.set(0)
        works_2 = self.works.duplicate()
        self.scatter_l2g(x, self.works, PETSc.InsertMode.INSERT_VALUES, PETSc.ScatterMode.SCATTER_REVERSE)
        self.Aposs.mult(self.works,works_2)
        self.scatter_l2g(works_2, y, PETSc.InsertMode.ADD_VALUES)

class Apos_ctx(object):
    def __init__(self,A_mpiaij, Aneg):
        self.A_mpiaij = A_mpiaij
        self.Aneg = Aneg
    def mult(self, mat, x, y):
        xtemp = x.duplicate()
        self.Aneg.mult(x,xtemp)
        self.A_mpiaij.mult(x,y)
        y += xtemp

class Anegs_ctx(object):
    def __init__(self, Vnegs, DVnegs):
        self.Vnegs = Vnegs
        self.DVnegs = DVnegs
        self.gamma = PETSc.Vec().create(comm=PETSc.COMM_SELF)
        self.gamma.setType(PETSc.Vec.Type.SEQ)
        self.gamma.setSizes(len(self.Vnegs))
    def mult(self, mat, x, y):
        y.set(0)
        for i,vec in enumerate(self.DVnegs):
            self.gamma[i] = x.dot(vec)
        for i,vec in enumerate(self.Vnegs):
            #if mpi.COMM_WORLD.rank == 0:
            #    print(f'self.gamma[i]: {self.gamma[i]}')
            y.axpy(self.gamma[i], vec)


class Aposs_ctx(object):
    def __init__(self,Bs, Anegs):
        self.Bs = Bs
        self.Anegs = Anegs
    def mult(self, mat, x, y):
        xtemp = x.duplicate()
        self.Anegs.mult(x,xtemp)
        self.Bs.mult(x,y)
        y += xtemp

class scaledmats_ctx(object):
    def __init__(self, mats, musl, musr):
        self.mats = mats
        self.musl = musl
        self.musr = musr
    def mult(self, mat, x, y):
        xtemp = x.copy()*self.musr
        self.mats.mult(xtemp,y)
        y *= self.musl
    def apply(self, mat, x, y):
        self.mult(mat, x, y)

class invAposs_ctx(object):
    def __init__(self,Bs_ksp,projVposs):
        self.Bs_ksp = Bs_ksp
        self.projVposs = projVposs
    def apply(self, mat, x, y):
        xtemp1 = y.duplicate()
        xtemp2 = y.duplicate()
        self.projVposs.mult(x,xtemp1)
        self.Bs_ksp.solve(xtemp1,xtemp2)
        self.projVposs.mult(xtemp2,y)
    def mult(self, mat, x, y):
        #xtemp1 = y.duplicate()
        #xtemp2 = y.duplicate()
        #self.projVnegs.mult(x,xtemp1)
        #self.Bs_ksp.solve(xtemp1,xtemp2)
        #self.projVnegs.mult(xtemp2,y)
        self.apply(mat, x, y)

class projVnegs_ctx(object):
    def __init__(self, Vnegs):
        self.Vnegs = Vnegs
    def mult(self, mat, x, y):
        y.set(0)
        for i,vec in enumerate(self.Vnegs):
            y.axpy(x.dot(vec) , vec)

class projVposs_ctx(object):
    def __init__(self, projVnegs):
        self.projVnegs = projVnegs
    def mult(self, mat, x, y):
        self.projVnegs(-x,y)
        y.axpy(1.,x) 
