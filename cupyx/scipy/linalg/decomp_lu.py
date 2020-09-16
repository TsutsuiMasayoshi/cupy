from warnings import warn

import numpy

import cupy
from cupy.cuda import cublas
from cupy.cuda import cusolver
from cupy.cuda import device
from cupy.linalg import util


def lu_factor(a, overwrite_a=False, check_finite=True):
    """LU decomposition.

    Decompose a given two-dimensional square matrix into ``P * L * U``,
    where ``P`` is a permutation matrix,  ``L`` lower-triangular with
    unit diagonal elements, and ``U`` upper-triangular matrix.
    Note that in the current implementation ``a`` must be
    a real matrix, and only :class:`numpy.float32` and :class:`numpy.float64`
    are supported.

    Args:
        a (cupy.ndarray): The input matrix with dimension ``(M, N)``
        overwrite_a (bool): Allow overwriting data in ``a`` (may enhance
            performance)
        check_finite (bool): Whether to check that the input matrices contain
            only finite numbers. Disabling may give a performance gain, but may
            result in problems (crashes, non-termination) if the inputs do
            contain infinities or NaNs.

    Returns:
        tuple:
            ``(lu, piv)`` where ``lu`` is a :class:`cupy.ndarray`
            storing ``U`` in its upper triangle, and ``L`` without
            unit diagonal elements in its lower triangle, and ``piv`` is
            a :class:`cupy.ndarray` storing pivot indices representing
            permutation matrix ``P``. For ``0 <= i < min(M,N)``, row
            ``i`` of the matrix was interchanged with row ``piv[i]``

    .. seealso:: :func:`scipy.linalg.lu_factor`

    .. note::

        Current implementation returns result different from SciPy when the
        matrix singular. SciPy returns an array containing ``0.`` while the
        current implementation returns an array containing ``nan``.

        >>> import numpy as np
        >>> import scipy.linalg
        >>> scipy.linalg.lu_factor(np.array([[0, 1], [0, 0]], \
dtype=np.float32))
        (array([[0., 1.],
               [0., 0.]], dtype=float32), array([0, 1], dtype=int32))

        >>> import cupy as cp
        >>> import cupyx.scipy.linalg
        >>> cupyx.scipy.linalg.lu_factor(cp.array([[0, 1], [0, 0]], \
dtype=cp.float32))
        (array([[ 0.,  1.],
               [nan, nan]], dtype=float32), array([0, 1], dtype=int32))
    """
    return _lu_factor(a, overwrite_a, check_finite)


def lu(a, permute_l=False, overwrite_a=False, check_finite=True):
    """LU decomposition.

    Args:
        a (cupy.ndarray): The input matrix with dimension ``(M, N)``
        permute_l (bool): If ``True``, perform the multiplication ``P * L``.
        overwrite_a (bool): Allow overwriting data in ``a`` (may enhance
            performance)
        check_finite (bool): Whether to check that the input matrices contain
            only finite numbers. Disabling may give a performance gain, but may
            result in problems (crashes, non-termination) if the inputs do
            contain infinities or NaNs.
        a (cupy.ndarray): Array to decompose

    Returns:
        **(If permute_l == False)**
        p (cupy.ndarray): Permutation matrix with dimension ``(M, M)``
        l (cupy.ndarray): Lower triangular or trapezoidal matrix with unit
            diagonal with dimension ``(M, K)``. ``K = min(M, N)``.
        u (cupy.ndarray): Upper triangular or trapezoidal matrix with dimension
            ``(K, N)``. ``K = min(M, N)``.

        **(If permute_l == True)**
        pl (cupy.ndarray): Permuted ``L`` matrix. ``K = min(M, N)``.
        u (cupy.ndarray): Upper triangular or trapezoidal matrix with dimension
            ``(K, N)``. ``K = min(M, N)``.

    .. seealso:: :func:`scipy.linalg.lu`
    """
    lu, piv = _lu_factor(a, overwrite_a, check_finite)

    m, n = lu.shape
    k = min(m, n)
    L = cupy.empty((m, k), dtype=lu.dtype)
    U = cupy.empty((k, n), dtype=lu.dtype)
    size = m * n
    cupy_split_lu()(lu, m, n, k, L, U, size=size)

    if permute_l:
        cupy_laswp()(m, k, k, piv, L, size=k)
        return (L, U)
    else:
        r_dtype = numpy.float32 if lu.dtype.char in 'fF' else numpy.float64
        P = cupy.diag(cupy.ones((m,), dtype=r_dtype))
        cupy_laswp()(m, m, k, piv, P, size=m)
        return (P, L, U)


def _lu_factor(a, overwrite_a=False, check_finite=True):
    a = cupy.asarray(a)
    util._assert_rank2(a)

    dtype = a.dtype

    if dtype.char == 'f':
        getrf = cusolver.sgetrf
        getrf_bufferSize = cusolver.sgetrf_bufferSize
    elif dtype.char == 'd':
        getrf = cusolver.dgetrf
        getrf_bufferSize = cusolver.dgetrf_bufferSize
    elif dtype.char == 'F':
        getrf = cusolver.cgetrf
        getrf_bufferSize = cusolver.cgetrf_bufferSize
    elif dtype.char == 'D':
        getrf = cusolver.zgetrf
        getrf_bufferSize = cusolver.zgetrf_bufferSize
    else:
        raise ValueError('unsupported dtype (actual:{})'.format(dtype))

    a = a.astype(dtype, order='F', copy=(not overwrite_a))

    if check_finite:
        if a.dtype.kind == 'f' and not cupy.isfinite(a).all():
            raise ValueError(
                'array must not contain infs or NaNs')

    cusolver_handle = device.get_cusolver_handle()
    dev_info = cupy.empty(1, dtype=numpy.int32)

    m, n = a.shape

    ipiv = cupy.empty((min(m, n),), dtype=numpy.intc)

    buffersize = getrf_bufferSize(cusolver_handle, m, n, a.data.ptr, m)
    workspace = cupy.empty(buffersize, dtype=dtype)

    # LU factorization
    getrf(cusolver_handle, m, n, a.data.ptr, m, workspace.data.ptr,
          ipiv.data.ptr, dev_info.data.ptr)

    if dev_info[0] < 0:
        raise ValueError('illegal value in %d-th argument of '
                         'internal getrf (lu_factor)' % -dev_info[0])
    elif dev_info[0] > 0:
        warn('Diagonal number %d is exactly zero. Singular matrix.'
             % dev_info[0], RuntimeWarning, stacklevel=2)

    # cuSolver uses 1-origin while SciPy uses 0-origin
    ipiv -= 1

    return (a, ipiv)


def cupy_split_lu():
    return cupy.ElementwiseKernel(
        'raw T LU, int32 M, int32 N, int32 K',
        'raw T L, raw T U',
        '''
        // LU: shape: (M, N), column major matrix
        // L: shape: (M, K), row major matrix
        // U: shape: (K, N), row major matrix
        const T* ptr_LU = &(LU[0]);
        T* ptr_L = &(L[0]);
        T* ptr_U = &(U[0]);
        int row = i / N;
        int col = i % N;
        T lu_val = ptr_LU[row + col * M];
        T l_val, u_val;
        if (row > col) {
            l_val = lu_val;
            u_val = static_cast<T>(0);
        } else if (row == col) {
            l_val = static_cast<T>(1);
            u_val = lu_val;
        } else {
            l_val = static_cast<T>(0);
            u_val = lu_val;
        }
        if (col < K) ptr_L[col + row * K] = l_val;
        if (row < K) ptr_U[col + row * N] = u_val;
        ''',
        'cupy_split_lu'
    )


def cupy_laswp():
    return cupy.ElementwiseKernel(
        'int32 M, int32 N, int32 K, raw I PIV',
        'raw T A',
        '''
        // PIV: shape: (K,), 0-based
        // A: shape: (M, N), row major matrix
        T* ptr_A = &(A[0]);
        int col = i;
        for (int row_k = 0; row_k < K; row_k++) {
            int row_j = PIV[row_k];
            if (row_j <= row_k) continue;
            // swap elements in row_k and row_j
            T tmp = ptr_A[col + row_k * N];
            ptr_A[col + row_k * N] = ptr_A[col + row_j * N];
            ptr_A[col + row_j * N] = tmp;
        }
        ''',
        'cupy_laswp'
    )


def lu_solve(lu_and_piv, b, trans=0, overwrite_b=False, check_finite=True):
    """Solve an equation system, ``a * x = b``, given the LU factorization of ``a``

    Args:
        lu_and_piv (tuple): LU factorization of matrix ``a`` (``(M, M)``)
            together with pivot indices.
        b (cupy.ndarray): The matrix with dimension ``(M,)`` or
            ``(M, N)``.
        trans ({0, 1, 2}): Type of system to solve:

            ========  =========
            trans     system
            ========  =========
            0         a x  = b
            1         a^T x = b
            2         a^H x = b
            ========  =========
        overwrite_b (bool): Allow overwriting data in b (may enhance
            performance)
        check_finite (bool): Whether to check that the input matrices contain
            only finite numbers. Disabling may give a performance gain, but may
            result in problems (crashes, non-termination) if the inputs do
            contain infinities or NaNs.

    Returns:
        cupy.ndarray:
            The matrix with dimension ``(M,)`` or ``(M, N)``.

    .. seealso:: :func:`scipy.linalg.lu_solve`
    """

    (lu, ipiv) = lu_and_piv

    util._assert_cupy_array(lu)
    util._assert_rank2(lu)
    util._assert_nd_squareness(lu)

    m = lu.shape[0]
    if m != b.shape[0]:
        raise ValueError('incompatible dimensions.')

    dtype = lu.dtype
    if dtype.char == 'f':
        getrs = cusolver.sgetrs
    elif dtype.char == 'd':
        getrs = cusolver.dgetrs
    elif dtype.char == 'F':
        getrs = cusolver.cgetrs
    elif dtype.char == 'D':
        getrs = cusolver.zgetrs
    else:
        raise ValueError('unsupported dtype (actual:{})'.format(lu.dtype))

    if trans == 0:
        trans = cublas.CUBLAS_OP_N
    elif trans == 1:
        trans = cublas.CUBLAS_OP_T
    elif trans == 2:
        trans = cublas.CUBLAS_OP_C
    else:
        raise ValueError('unknown trans')

    lu = lu.astype(dtype, order='F', copy=False)
    ipiv = ipiv.astype(ipiv.dtype, order='F', copy=True)
    # cuSolver uses 1-origin while SciPy uses 0-origin
    ipiv += 1
    b = b.astype(dtype, order='F', copy=(not overwrite_b))

    if check_finite:
        if lu.dtype.kind == 'f' and not cupy.isfinite(lu).all():
            raise ValueError(
                'array must not contain infs or NaNs.\n'
                'Note that when a singular matrix is given, unlike '
                'scipy.linalg.lu_factor, cupyx.scipy.linalg.lu_factor '
                'returns an array containing NaN.')
        if b.dtype.kind == 'f' and not cupy.isfinite(b).all():
            raise ValueError(
                'array must not contain infs or NaNs')

    n = 1 if b.ndim == 1 else b.shape[1]
    cusolver_handle = device.get_cusolver_handle()
    dev_info = cupy.empty(1, dtype=numpy.int32)

    # solve for the inverse
    getrs(cusolver_handle,
          trans,
          m, n, lu.data.ptr, m, ipiv.data.ptr, b.data.ptr,
          m, dev_info.data.ptr)

    if dev_info[0] < 0:
        raise ValueError('illegal value in %d-th argument of '
                         'internal getrs (lu_solve)' % -dev_info[0])

    return b
