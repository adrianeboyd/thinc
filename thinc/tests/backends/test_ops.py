import pytest
import numpy
from hypothesis import given, settings
from hypothesis.strategies import composite, integers
from numpy.testing import assert_allclose
from thinc.api import NumpyOps, CupyOps, Ops, get_ops
from thinc.api import get_current_ops, use_ops
from thinc.util import has_torch
from thinc.api import fix_random_seed
from thinc.api import LSTM
import inspect

from .. import strategies
from ..strategies import ndarrays_of_shape


MAX_EXAMPLES = 10

VANILLA_OPS = Ops(numpy)  # type:ignore
NUMPY_OPS = NumpyOps()
BLIS_OPS = NumpyOps(use_blis=True)
CPU_OPS = [NUMPY_OPS, VANILLA_OPS]
XP_OPS = [NUMPY_OPS]
if CupyOps.xp is not None:
    XP_OPS.append(CupyOps())
ALL_OPS = XP_OPS + [VANILLA_OPS]


def create_pytorch_funcs():
    import torch
    import math

    def torch_relu_k(x):
        return torch.nn.functional.relu6(x)

    def torch_hard_sigmoid(x):
        return torch.clip(x * 0.2 + 0.5, 0, 1)

    def torch_hard_tanh(x):
        return torch.nn.functional.hardtanh(x)

    def torch_swish(x):
        return torch.nn.functional.silu(x)

    def torch_hard_swish(x):
        return x * torch_hard_sigmoid(x)

    def torch_hard_swish_mobilenet(x):
        return torch.nn.functional.hardswish(x)

    # https://github.com/huggingface/transformers/blob/master/src/transformers/activations.py#L37
    def torch_gelu_approx(x):
        return (
            0.5
            * x
            * (
                1.0
                + torch.tanh(
                    math.sqrt(2.0 / math.pi) * (x + 0.044715 * torch.pow(x, 3.0))
                )
            )
        )

    def torch_gelu(x):
        return torch.nn.functional.gelu(x)

    return [
        ("relu_k", torch_relu_k),
        ("hard_sigmoid", torch_hard_sigmoid),
        ("hard_tanh", torch_hard_tanh),
        ("swish", torch_swish),
        ("hard_swish", torch_hard_swish),
        ("hard_swish_mobilenet", torch_hard_swish_mobilenet),
        ("gelu_approx", torch_gelu_approx),
        ("gelu", torch_gelu),
    ]


if has_torch:
    TORCH_FUNCS = create_pytorch_funcs()
else:
    TORCH_FUNCS = []


@pytest.mark.parametrize("op", [NumpyOps, CupyOps])
def test_ops_consistency(op):
    """Test that specific ops don't define any methods that are not on the
    Ops base class and that all ops methods define the exact same arguments."""
    attrs = [m for m in dir(op) if not m.startswith("_")]
    for attr in attrs:
        assert hasattr(Ops, attr)
        method = getattr(op, attr)
        if hasattr(method, "__call__"):
            sig = inspect.signature(method)
            params = [p for p in sig.parameters][1:]
            base_sig = inspect.signature(getattr(Ops, attr))
            base_params = [p for p in base_sig.parameters][1:]
            assert params == base_params, attr
            defaults = [p.default for p in sig.parameters.values()][1:]
            base_defaults = [p.default for p in base_sig.parameters.values()][1:]
            assert defaults == base_defaults, attr
            # If args are type annotated, their types should be the same
            annots = [p.annotation for p in sig.parameters.values()][1:]
            base_annots = [p.annotation for p in base_sig.parameters.values()][1:]
            for i, (p1, p2) in enumerate(zip(annots, base_annots)):
                if p1 != inspect.Parameter.empty and p2 != inspect.Parameter.empty:
                    # Need to check string value to handle TypeVars etc.
                    assert str(p1) == str(p2), attr


@pytest.mark.parametrize("ops", ALL_OPS)
def test_alloc(ops):
    float_methods = (ops.alloc1f, ops.alloc2f, ops.alloc3f, ops.alloc4f)
    for i, method in enumerate(float_methods):
        shape = (1,) * (i + 1)
        arr = method(*shape)
        assert arr.dtype == numpy.float32
        assert arr.ndim == len(shape)
        arr = ops.alloc_f(shape)
        assert arr.dtype == numpy.float32
        assert arr.ndim == len(shape)
    int_methods = (ops.alloc1i, ops.alloc2i, ops.alloc3i, ops.alloc4i)
    for i, method in enumerate(int_methods):
        shape = (1,) * (i + 1)
        arr = method(*shape)
        assert arr.dtype == numpy.int32
        assert arr.ndim == len(shape)
        arr = ops.alloc_i(shape)
        assert arr.dtype == numpy.int32
        assert arr.ndim == len(shape)
    assert ops.alloc(1).ndim == 1


@pytest.mark.parametrize("ops", XP_OPS)
def test_hash_gives_distinct_keys(ops):
    ids = ops.alloc1f(5, dtype="uint64")
    keys = ops.hash(ids, 0)
    assert keys.shape == (5, 4)
    assert keys.dtype == "uint32"
    for i in range(len(ids)):
        for j in range(keys.shape[1]):
            assert keys[i, j] != 0


@pytest.mark.parametrize("ops", XP_OPS)
def test_get_dropout_empty(ops):
    shape = (2, 2)
    drop = 0.0
    mask = ops.get_dropout_mask(shape, drop)
    if drop <= 0.0:
        assert mask[mask == 1.0].all()
    else:
        assert mask[mask != 1.0].all()


@pytest.mark.parametrize("ops", XP_OPS)
def test_get_dropout_not_empty(ops):
    shape = (200, 200)
    drop = 0.5
    mask = ops.get_dropout_mask(shape, drop)
    assert (mask > 1.0).any()
    assert (mask == 0.0).any()
    assert mask.shape == shape


@pytest.mark.parametrize("ops", CPU_OPS)
def test_seq2col_window_one_small(ops):
    seq = ops.asarray([[1.0], [3.0], [4.0], [5]], dtype="float32")
    cols = ops.seq2col(seq, 1)
    if hasattr(cols, "get"):
        cols = cols.get()
    assert_allclose(cols[0], [0.0, 1.0, 3.0])
    assert_allclose(cols[1], [1.0, 3.0, 4.0])
    assert_allclose(cols[2], [3.0, 4.0, 5.0])
    assert_allclose(cols[3], [4.0, 5.0, 0.0])


@pytest.mark.parametrize("ops", XP_OPS)
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(X=strategies.arrays_BOP())
def test_maxout(ops, X):
    X = ops.asarray(X)
    expected_best = X.max(axis=-1)
    predicted_best, which = ops.maxout(X)
    ops.xp.testing.assert_allclose(
        expected_best, predicted_best, rtol=0.001, atol=0.001
    )
    # Can't compare 'which' directly, as sort order might be different
    # We could check that using the 'which', we get the right results?


@pytest.mark.parametrize("ops", ALL_OPS)
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(X=strategies.arrays_BI())
def test_seq2col_window_one(ops, X):
    X = ops.asarray(X)
    base_ops = Ops()
    base_ops.xp = ops.xp
    baseX = base_ops.alloc(X.shape) + X
    target = base_ops.seq2col(base_ops.asarray(baseX), nW=1)
    predicted = ops.seq2col(X, nW=1)
    ops.xp.testing.assert_allclose(target, predicted, atol=0.001, rtol=0.001)


@pytest.mark.parametrize("ops", XP_OPS)
def test_seq2col_lengths_all_zero(ops):
    # Empty batch
    ops.xp.testing.assert_allclose(
        ops.alloc((0, 0)),
        ops.seq2col(ops.alloc((0, 0)), 1, lengths=ops.xp.zeros((0,), dtype="int32")),
    )

    ops.xp.testing.assert_allclose(
        ops.alloc((0, 0)),
        ops.backprop_seq2col(
            ops.alloc((0, 0)), 1, lengths=ops.xp.zeros((0,), dtype="int32")
        ),
    )

    # Zero-length sequence
    ops.xp.testing.assert_allclose(
        ops.alloc((0, 0)), ops.seq2col(ops.alloc((0, 0)), 1, lengths=ops.asarray1i([0]))
    )

    ops.xp.testing.assert_allclose(
        ops.alloc((0, 0)),
        ops.backprop_seq2col(ops.alloc((0, 0)), 1, lengths=ops.asarray1i([0])),
    )

    # Multiple zero-length sequences
    ops.xp.testing.assert_allclose(
        ops.alloc((0, 0)),
        ops.seq2col(ops.alloc((0, 0)), 1, lengths=ops.asarray1i([0, 0])),
    )

    ops.xp.testing.assert_allclose(
        ops.alloc((0, 0)),
        ops.backprop_seq2col(ops.alloc((0, 0)), 1, lengths=ops.asarray1i([0, 0])),
    )


@pytest.mark.parametrize("ops", XP_OPS)
def test_seq2col_lengths_zero_first_last(ops):
    cols_check = ops.asarray2f(
        [
            [0, 0, 0, 1, 2, 3, 4, 5, 6],
            [1, 2, 3, 4, 5, 6, 7, 8, 9],
            [4, 5, 6, 7, 8, 9, 10, 11, 12],
            [7, 8, 9, 10, 11, 12, 13, 14, 15],
            [10, 11, 12, 13, 14, 15, 0, 0, 0],
        ]
    )

    grad_check = ops.asarray2f(
        [[2, 4, 6], [12, 15, 18], [21, 24, 27], [30, 33, 36], [26, 28, 30]]
    )

    # Initial zero-length sequence
    ops.xp.testing.assert_allclose(
        cols_check,
        ops.seq2col(
            ops.xp.arange(1.0, 16.0, dtype="float32").reshape(5, 3),
            1,
            lengths=ops.asarray1i([0, 5]),
        ),
    )

    ops.xp.testing.assert_allclose(
        grad_check,
        ops.backprop_seq2col(
            cols_check,
            1,
            lengths=ops.asarray1i([0, 5]),
        ),
    )

    # Final zero-length sequence.
    ops.xp.testing.assert_allclose(
        cols_check,
        ops.seq2col(
            ops.xp.arange(1.0, 16.0, dtype="float32").reshape(5, 3),
            1,
            lengths=ops.asarray1i([5, 0]),
        ),
    )


@pytest.mark.parametrize("ops", XP_OPS)
def test_seq2col_lengths_zero_between(ops):
    cols_check = ops.asarray2f(
        [
            [0, 0, 0, 1, 2, 3, 4, 5, 6],
            [1, 2, 3, 4, 5, 6, 7, 8, 9],
            [4, 5, 6, 7, 8, 9, 10, 11, 12],
            [7, 8, 9, 10, 11, 12, 13, 14, 15],
            [10, 11, 12, 13, 14, 15, 0, 0, 0],
            [0, 0, 0, 16, 17, 18, 19, 20, 21],
            [16, 17, 18, 19, 20, 21, 0, 0, 0],
        ]
    )

    grad_check = ops.asarray2f(
        [
            [2, 4, 6],
            [12, 15, 18],
            [21, 24, 27],
            [30, 33, 36],
            [26, 28, 30],
            [32, 34, 36],
            [38, 40, 42],
        ]
    )

    # Zero-length between.
    ops.xp.testing.assert_allclose(
        cols_check,
        ops.seq2col(
            ops.xp.arange(1.0, 22.0, dtype="float32").reshape(7, 3),
            1,
            lengths=ops.asarray1i([5, 0, 2]),
        ),
    )

    ops.xp.testing.assert_allclose(
        grad_check,
        ops.backprop_seq2col(
            cols_check,
            1,
            lengths=ops.asarray1i([5, 0, 2]),
        ),
    )

    # Zero-length between twice.
    ops.xp.testing.assert_allclose(
        cols_check,
        ops.seq2col(
            ops.xp.arange(1.0, 22.0, dtype="float32").reshape(7, 3),
            1,
            lengths=ops.asarray1i([5, 0, 0, 2]),
        ),
    )

    ops.xp.testing.assert_allclose(
        grad_check,
        ops.backprop_seq2col(
            cols_check,
            1,
            lengths=ops.asarray1i([5, 0, 0, 2]),
        ),
    )


@pytest.mark.parametrize("ops", XP_OPS)
def test_seq2col_window_one_lengths(ops):
    X = ops.xp.arange(1.0, 16.0, dtype="float32").reshape(5, 3)
    lengths = ops.asarray1i([1, 3, 1])
    cols = ops.seq2col(X, 1, lengths=lengths)
    ops.xp.testing.assert_allclose(
        ops.asarray2f(
            [
                [0, 0, 0, 1, 2, 3, 0, 0, 0],
                [0, 0, 0, 4, 5, 6, 7, 8, 9],
                [4, 5, 6, 7, 8, 9, 10, 11, 12],
                [7, 8, 9, 10, 11, 12, 0, 0, 0],
                [0, 0, 0, 13, 14, 15, 0, 0, 0],
            ]
        ),
        cols,
    )


@pytest.mark.parametrize("ops", XP_OPS)
def test_seq2col_window_two_lengths(ops):
    X = ops.xp.arange(1.0, 16.0, dtype="float32").reshape(5, 3)
    lengths = ops.asarray1i([1, 3, 1])
    cols = ops.seq2col(X, 2, lengths=lengths)
    ops.xp.testing.assert_allclose(
        ops.asarray2f(
            [
                [0, 0, 0, 0, 0, 0, 1, 2, 3, 0, 0, 0, 0, 0, 0],
                [0, 0, 0, 0, 0, 0, 4, 5, 6, 7, 8, 9, 10, 11, 12],
                [0, 0, 0, 4, 5, 6, 7, 8, 9, 10, 11, 12, 0, 0, 0],
                [4, 5, 6, 7, 8, 9, 10, 11, 12, 0, 0, 0, 0, 0, 0],
                [0, 0, 0, 0, 0, 0, 13, 14, 15, 0, 0, 0, 0, 0, 0],
            ]
        ),
        cols,
    )


@pytest.mark.parametrize("ops", ALL_OPS)
def test_backprop_seq2col_window_one_small(ops):
    cols = ops.asarray(
        [[0.0, 0.0, 0.0], [-1.0, 0.0, 1.0], [2.0, 0.0, 0.0]], dtype="float32"
    )
    expected = [[-1.0], [2.0], [1.0]]
    seq = ops.backprop_seq2col(cols, 1)
    if not isinstance(seq, numpy.ndarray):
        seq = seq.get()
    assert_allclose(seq, expected, atol=0.001, rtol=0.001)


@pytest.mark.parametrize("ops", ALL_OPS)
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(X=strategies.arrays_BI())
def test_backprop_seq2col_window_one(ops, X):
    if X.shape[1] % 3:
        return None
    X = ops.asarray(X)
    if ops.xp.abs(X).max() >= 30:
        return None
    base_ops = Ops()
    base_ops.xp = ops.xp
    target = base_ops.backprop_seq2col(X, nW=1)
    predicted = ops.backprop_seq2col(X, nW=1)
    for row in range(target.shape[0]):
        diff = target[row].sum() - predicted[row].sum()
        if diff < -0.1 or diff > 0.1:
            print(row, diff)
            print(target[row])
            print(predicted[row])
    ops.xp.testing.assert_allclose(target, predicted, atol=0.001, rtol=0.001)


@pytest.mark.parametrize("ops", XP_OPS)
def test_backprop_seq2col_window_one_lengths(ops):
    d_y = ops.xp.arange(0.1, 4.6, step=0.1, dtype="float32").reshape(5, 9)
    lengths = ops.asarray1i([1, 3, 1])
    d_seqs = ops.backprop_seq2col(d_y, 1, lengths=lengths)

    ops.xp.testing.assert_allclose(
        ops.asarray2f(
            [
                [0.4, 0.5, 0.6],
                [3.2, 3.4, 3.6],
                [6.6, 6.9, 7.2],
                [5.6, 5.8, 6.0],
                [4.0, 4.1, 4.2],
            ]
        ),
        d_seqs,
        atol=1e-6,
    )


@pytest.mark.parametrize("ops", XP_OPS)
def test_seq2col_window_two(ops):
    seq = ops.asarray([[1.0], [2.0], [3.0], [4]], dtype="float32")
    cols = ops.seq2col(seq, 2)
    if not isinstance(cols, numpy.ndarray):
        cols = cols.get()
    assert_allclose(cols[0], [0.0, 0.0, 1.0, 2.0, 3.0])
    assert_allclose(cols[1], [0.0, 1.0, 2.0, 3.0, 4.0])
    assert_allclose(cols[2], [1.0, 2.0, 3.0, 4.0, 0.0])
    assert_allclose(cols[3], [2.0, 3.0, 4.0, 0.0, 0.0])


@pytest.mark.parametrize("ops", XP_OPS)
def test_backprop_seq2col_window_two_lengths(ops):
    d_y = ops.xp.arange(0.1, 7.6, step=0.1, dtype="float32").reshape(5, 15)
    lengths = ops.asarray1i([1, 3, 1])
    d_seqs = ops.backprop_seq2col(d_y, 2, lengths=lengths)

    ops.xp.testing.assert_allclose(
        ops.asarray2f(
            [
                [0.7, 0.8, 0.9],
                [10.2, 10.5, 10.8],
                [11.1, 11.4, 11.7],
                [12.0, 12.3, 12.6],
                [6.7, 6.8, 6.9],
            ]
        ),
        d_seqs,
    )


@pytest.mark.parametrize("ops", XP_OPS)
def test_backprop_seq2col_window_two(ops):
    cols = ops.asarray(
        [
            [0.0, 0.0, 1.0, 2.0, 3.0],
            [0.0, 1.0, 2.0, 3.0, 4.0],
            [1.0, 2.0, 3.0, 4.0, 0.0],
            [2.0, 3.0, 4.0, 0.0, 0.0],
        ],
        dtype="float32",
    )
    # We're summing the values that each row
    # was used as a feature. So row 0 had a
    # gradient of 1 in row 0, 1 in row 2, and
    # 1 in row 3.
    expected = ops.asarray(
        [
            [1 + 1 + 1.0 + 0.0],
            [2.0 + 2.0 + 2.0 + 2.0],
            [3.0 + 3.0 + 3.0 + 3.0],
            [0.0 + 4.0 + 4.0 + 4.0],
        ],
        dtype="f",
    )
    seq = ops.backprop_seq2col(cols, 2)
    ops.xp.testing.assert_allclose(seq, expected, atol=0.001, rtol=0.001)


@pytest.mark.skipif(CupyOps.xp is None, reason="needs GPU/CuPy")
@pytest.mark.parametrize("nW", [1, 2])
def test_large_seq2col_gpu_against_cpu(nW):
    cupy_ops = CupyOps()
    numpy_ops = NumpyOps()

    # Use array with a large enough batch to require multiple
    # CUDA grids.
    batch_size = 128 * 128 * 2  # threads per block * blocks * 2
    X = numpy_ops.xp.random.randn(batch_size * 2).astype("float32").reshape(-1, 2)
    X_gpu = cupy_ops.asarray2f(X)

    # Use somewhat interesting sequence lengths.
    lengths = numpy_ops.asarray1i([1, 4, 2, 1] * (batch_size // 8))
    lengths_gpu = cupy_ops.asarray1i(lengths)

    cols = numpy_ops.seq2col(X, nW=nW, lengths=lengths)
    cols_gpu = cupy_ops.seq2col(X_gpu, nW=nW, lengths=lengths_gpu)

    assert_allclose(cols, cols_gpu.get())


@pytest.mark.skipif(CupyOps.xp is None, reason="needs GPU/CuPy")
@pytest.mark.parametrize("nW", [1, 2])
def test_large_backprop_seq2col_gpu_against_cpu(nW):
    cupy_ops = CupyOps()
    numpy_ops = NumpyOps()

    # Use array with a large enough batch to require multiple
    # CUDA grids.
    batch_size = 128 * 128 * 2  # threads per block * blocks * 2
    nF = 2 * nW + 1
    d_cols = (
        numpy_ops.xp.random.randn(batch_size * nF).astype("float32").reshape(-1, nF)
    )
    d_cols_gpu = cupy_ops.asarray2f(d_cols)

    # Use somewhat interesting sequence lengths.
    lengths = numpy_ops.asarray1i([1, 4, 2, 1] * (batch_size // 8))
    lengths_gpu = cupy_ops.asarray1i(lengths)

    d_seqs = numpy_ops.backprop_seq2col(d_cols, nW=nW, lengths=lengths)
    d_seqs_gpu = cupy_ops.backprop_seq2col(d_cols_gpu, nW=nW, lengths=lengths_gpu)

    assert_allclose(d_seqs, d_seqs_gpu.get())


@pytest.mark.parametrize("ops", ALL_OPS)
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(X=strategies.arrays_BI())
def test_backprop_reduce_sum(ops, X):
    X = ops.asarray(X)
    if ops.xp.abs(X).max() >= 5:
        return None
    lengths = ops.asarray([3] * len(X), dtype="i")
    out = ops.backprop_reduce_sum(X, lengths)
    assert out.shape == (sum(lengths), X.shape[1])
    start = 0
    for i, length in enumerate(lengths):
        ops.xp.testing.assert_allclose(
            out[start : start + length].sum(axis=0), X[i] * length, rtol=0.01, atol=0.01
        )
        start += length


@pytest.mark.parametrize("ops", ALL_OPS)
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(X=strategies.arrays_BI())
def test_softmax_sums_to_one(ops, X):
    y = ops.softmax(ops.asarray(X))
    for row in y:
        assert 0.99999 <= row.sum() <= 1.0001


@pytest.mark.parametrize("ops", ALL_OPS)
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(X=strategies.arrays_BI())
def test_softmax_works_inplace(ops, X):
    X = ops.asarray(X)
    X = ops.softmax(X, inplace=True)
    for row in X:
        assert 0.99999 <= row.sum() <= 1.00001


@pytest.mark.parametrize("cpu_ops", [*CPU_OPS, BLIS_OPS])
def test_gemm_computes_correctly(cpu_ops):
    W = numpy.zeros((3, 2), dtype="f")
    X = numpy.zeros((4, 2), dtype="f")
    W += numpy.random.uniform(size=W.size).reshape(W.shape)
    X += numpy.random.uniform(size=X.size).reshape(X.shape)
    Y = cpu_ops.gemm(X, W, trans2=True)
    expected = numpy.dot(X, W.T)
    assert_allclose(expected, Y, atol=1e-4, rtol=1e-4)
    W = numpy.zeros((2, 3), dtype="f")
    X = numpy.zeros((2, 4), dtype="f")
    W += numpy.random.uniform(size=W.size).reshape(W.shape)
    X += numpy.random.uniform(size=X.size).reshape(X.shape)
    Y = cpu_ops.gemm(X, W, trans1=True)
    expected = numpy.dot(X.T, W)
    assert_allclose(expected, Y, atol=1e-4, rtol=1e-4)
    cpu_ops.gemm(X, W, trans1=True, out=Y)


@pytest.mark.parametrize("cpu_ops", [*CPU_OPS, BLIS_OPS])
def test_gemm_out_used(cpu_ops):
    a = b = numpy.zeros((2, 2), dtype="f")
    c = numpy.ones((2, 2), dtype="f")
    cpu_ops.gemm(a, b, out=c)
    assert numpy.array_equal(c, numpy.zeros((2, 2)))


@pytest.mark.parametrize("cpu_ops", CPU_OPS)
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(X=strategies.arrays_BI())
def test_flatten_unflatten_roundtrip(cpu_ops, X):
    flat = cpu_ops.flatten([x for x in X])
    assert flat.ndim == 1
    unflat = cpu_ops.unflatten(flat, [len(x) for x in X])
    assert_allclose(X, unflat)
    flat2 = cpu_ops.flatten([x for x in X], pad=1, dtype="f")
    assert len(flat2) > len(flat)
    unflat2 = cpu_ops.unflatten(flat2, [len(x) for x in X], pad=1)
    assert_allclose(X, unflat2)


@pytest.mark.parametrize("ops", ALL_OPS)
def test_reduce_sum(ops):
    m = ops.xp.zeros((19, 5), dtype="f")
    m += 1
    lengths = ops.xp.array([5, 5, 3, 6], dtype="i")
    output = ops.reduce_sum(m, lengths)
    assert output.sum() == m.sum(), (output.sum(), m.sum())


@pytest.mark.parametrize("ops", XP_OPS)
def test_reduce_max_sm(ops):
    X = ops.xp.zeros((6, 3), dtype="f")
    X += ops.xp.random.uniform(-1, 1, X.shape)
    lengths = ops.xp.array([2, 2, 2], dtype="i")
    maxes, which = ops.reduce_max(X, lengths)
    start = 0
    for i, length in enumerate(lengths):
        truth = X[start : start + length].max(axis=0)
        ops.xp.testing.assert_allclose(maxes[i], truth)
        start += length


@pytest.mark.parametrize("ops", XP_OPS)
def test_reduce_max(ops):
    m = ops.xp.zeros((19, 5), dtype="f")
    m += ops.xp.random.uniform(-1, 1, m.shape)
    lengths = ops.xp.array([5, 5, 3, 6], dtype="i")
    # m[4, 0] = 1
    # m[0, 1] = 2
    # m[1, 3] = 3
    maxes, which = ops.reduce_max(m, lengths)
    start = 0
    for i, length in enumerate(lengths):
        truth = m[start : start + length].max(axis=0)
        ops.xp.testing.assert_allclose(maxes[i], truth)
        start += length


@pytest.mark.parametrize("ops", ALL_OPS)
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(X=strategies.arrays_BI())
def test_mish(ops, X):
    X = ops.asarray(X)
    Y = ops.mish(X)
    assert Y.shape == X.shape
    assert not ops.xp.isnan(Y).any()


@pytest.mark.parametrize("ops", ALL_OPS)
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(X=strategies.arrays_BI())
def test_relu_k(ops, X):
    X = ops.asarray(X)
    Y = ops.relu_k(X)
    assert Y.shape == X.shape
    assert not ops.xp.isnan(Y).any()
    assert (Y >= 0).sum() == Y.size
    assert (Y <= 6.0).sum() == Y.size


@pytest.mark.parametrize("ops", ALL_OPS)
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(X=strategies.arrays_BI())
def test_swish(ops, X):
    X = ops.asarray(X)
    Y = ops.swish(X)
    assert Y.shape == X.shape
    assert not ops.xp.isnan(Y).any()


@pytest.mark.parametrize("ops", ALL_OPS)
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(X=strategies.arrays_BI())
def test_hard_sigmoid(ops, X):
    X = ops.asarray(X)
    Y = ops.hard_sigmoid(X)
    assert Y.shape == X.shape
    assert not ops.xp.isnan(Y).any()
    assert (Y >= 0).sum() == Y.size
    assert (Y <= 1.0).sum() == Y.size


@pytest.mark.parametrize("ops", ALL_OPS)
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(X=strategies.arrays_BI())
def test_hard_tanh(ops, X):
    X = ops.asarray(X)
    Y = ops.hard_tanh(X)
    assert Y.shape == X.shape
    assert not ops.xp.isnan(Y).any()
    assert (Y >= -1.0).sum() == Y.size
    assert (Y <= 1.0).sum() == Y.size


@pytest.mark.parametrize("ops", ALL_OPS)
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(X=strategies.arrays_BI())
def test_hard_swish(ops, X):
    X = ops.asarray(X)
    Y = ops.hard_swish(X)
    assert Y.shape == X.shape
    assert not ops.xp.isnan(Y).any()


@pytest.mark.parametrize("ops", ALL_OPS)
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(X=strategies.arrays_BI())
def test_hard_swish_mobilenet(ops, X):
    X = ops.asarray(X)
    Y = ops.hard_swish_mobilenet(X)
    assert Y.shape == X.shape
    assert not ops.xp.isnan(Y).any()


@pytest.mark.parametrize("ops", ALL_OPS)
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(X=strategies.arrays_BI())
def test_gelu_approx(ops, X):
    X = ops.asarray(X)
    Y = ops.gelu_approx(X)
    assert Y.shape == X.shape
    assert not ops.xp.isnan(Y).any()


@pytest.mark.parametrize("ops", ALL_OPS)
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(X=strategies.arrays_BI())
def test_gelu(ops, X):
    X = ops.asarray(X)
    Y = ops.gelu(X)
    assert Y.shape == X.shape
    assert not ops.xp.isnan(Y).any()


@pytest.mark.parametrize("ops", ALL_OPS)
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(X=strategies.arrays_BI())
def test_backprop_mish(ops, X):
    X = ops.asarray(X)
    # Test zero gradients result in 0 dX
    zeros = ops.alloc(X.shape)
    dX = ops.backprop_mish(zeros, X)
    assert dX.shape == X.shape
    assert (dX == 0).all()


def get_lstm_args(depth, dirs, nO, batch_size, nI, draw=None):

    if dirs == 1:
        n_params = (nO * 4) * nI + nO * 4 + nO * 4 * nO + nO * 4
        for _ in range(1, depth):
            n_params += nO * 4 * nO + nO * 4 + nO * 4 * nO + nO * 4
    else:
        n_params = (nO * 2) * nI + nO * 2 + nO * 2 * (nO // 2) + nO * 2
        for _ in range(1, depth):
            n_params += nO * 2 * nO + nO * 2 + nO * 2 * (nO // 2) + nO * 2
        n_params *= 2
    lstm = LSTM(nO, nI, depth=depth, bi=dirs >= 2).initialize()
    assert lstm.get_param("LSTM").size == n_params
    if draw:
        params = draw(ndarrays_of_shape(n_params))
        # For some reason this is crashing hypothesis?
        # size_at_t = draw(ndarrays_of_shape(shape=(batch_size,), lo=1, dtype="int32"))
        size_at_t = numpy.ones(shape=(batch_size,), dtype="int32")
        X = draw(ndarrays_of_shape((int(size_at_t.sum()), nI)))
    else:
        params = numpy.ones((n_params,), dtype="f")
        size_at_t = numpy.ones(shape=(batch_size,), dtype="int32")
        X = numpy.zeros(((int(size_at_t.sum()), nI)))
    H0 = numpy.zeros((depth, dirs, nO // dirs))
    C0 = numpy.zeros((depth, dirs, nO // dirs))
    return (params, H0, C0, X, size_at_t)


@composite
def draw_lstm_args(draw):
    depth = draw(integers(1, 4))
    dirs = draw(integers(1, 2))
    nO = draw(integers(1, 16)) * dirs
    batch_size = draw(integers(1, 6))
    nI = draw(integers(1, 16))
    return get_lstm_args(depth, dirs, nO, batch_size, nI, draw=draw)


@pytest.mark.parametrize("ops", XP_OPS)
@pytest.mark.parametrize(
    "depth,dirs,nO,batch_size,nI",
    [
        (1, 1, 1, 1, 1),
        (1, 1, 2, 1, 1),
        (1, 1, 2, 1, 2),
        (2, 1, 1, 1, 1),
        (2, 1, 2, 2, 2),
        (1, 2, 2, 1, 1),
        (2, 2, 2, 2, 2),
    ],
)
def test_lstm_forward_training(ops, depth, dirs, nO, batch_size, nI):
    reference_ops = Ops()
    params, H0, C0, X, size_at_t = get_lstm_args(depth, dirs, nO, batch_size, nI)
    reference = reference_ops.lstm_forward_training(params, H0, C0, X, size_at_t)
    Y, fwd_state = ops.lstm_forward_training(params, H0, C0, X, size_at_t)
    assert_allclose(fwd_state[2], reference[1][2], atol=1e-4, rtol=1e-3)
    assert_allclose(fwd_state[1], reference[1][1], atol=1e-4, rtol=1e-3)
    assert_allclose(Y, reference[0], atol=1e-4, rtol=1e-3)


@pytest.mark.parametrize("ops", XP_OPS)
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(args=draw_lstm_args())
def test_lstm_forward_training_fuzz(ops, args):
    params, H0, C0, X, size_at_t = args
    reference_ops = Ops()
    reference = reference_ops.lstm_forward_training(params, H0, C0, X, size_at_t)
    Y, fwd_state = ops.lstm_forward_training(params, H0, C0, X, size_at_t)
    assert_allclose(fwd_state[2], reference[1][2], atol=1e-4, rtol=1e-3)
    assert_allclose(fwd_state[1], reference[1][1], atol=1e-4, rtol=1e-3)
    assert_allclose(Y, reference[0], atol=1e-4, rtol=1e-3)


def test_get_ops():
    assert isinstance(get_ops("numpy"), NumpyOps)
    assert isinstance(get_ops("cupy"), CupyOps)
    # If Apple ops are available, "cpu" should return AppleOps or
    # NumpyOps otherwise.
    try:
        from thinc_apple_ops import AppleOps

        assert isinstance(get_ops("cpu"), AppleOps)
    except ImportError:
        assert isinstance(get_ops("cpu"), NumpyOps)
    # If BigEndian ops are available, "cpu" should return BigEndianOps or
    # NumpyOps otherwise.
    try:
        from thinc_bigendian_ops import BigEndianOps

        assert isinstance(get_ops("cpu"), BigEndianOps)
    except ImportError:
        assert isinstance(get_ops("cpu"), NumpyOps)
    with pytest.raises(ValueError):
        get_ops("blah")
    ops = Ops(numpy)
    assert ops.xp == numpy


def test_use_ops():
    class_ops = get_current_ops()
    with use_ops("numpy"):
        new_ops = get_current_ops()
        assert new_ops.name == "numpy"
    with use_ops("cupy"):
        new_ops = get_current_ops()
        assert new_ops.name == "cupy"
    new_ops = get_current_ops()
    assert class_ops.name == new_ops.name


def test_minibatch():
    fix_random_seed(0)
    ops = get_current_ops()
    items = [1, 2, 3, 4, 5, 6]
    batches = ops.minibatch(3, items)
    assert list(batches) == [[1, 2, 3], [4, 5, 6]]
    batches = ops.minibatch((i for i in (3, 2, 1)), items)
    assert list(batches) == [[1, 2, 3], [4, 5], [6]]
    batches = list(ops.minibatch(3, numpy.asarray(items)))
    assert isinstance(batches[0], numpy.ndarray)
    assert numpy.array_equal(batches[0], numpy.asarray([1, 2, 3]))
    assert numpy.array_equal(batches[1], numpy.asarray([4, 5, 6]))
    batches = list(ops.minibatch((i for i in (3, 2, 1)), items, shuffle=True))
    assert batches != [[1, 2, 3], [4, 5], [6]]
    assert len(batches[0]) == 3
    assert len(batches[1]) == 2
    assert len(batches[2]) == 1
    with pytest.raises(ValueError):
        ops.minibatch(10, (i for i in range(100)))
    with pytest.raises(ValueError):
        ops.minibatch(10, True)


def test_multibatch():
    fix_random_seed(0)
    ops = get_current_ops()
    arr1 = numpy.asarray([1, 2, 3, 4])
    arr2 = numpy.asarray([5, 6, 7, 8])
    batches = list(ops.multibatch(2, arr1, arr2))
    assert numpy.concatenate(batches).tolist() == [[1, 2], [5, 6], [3, 4], [7, 8]]
    batches = list(ops.multibatch(2, arr1, arr2, shuffle=True))
    assert len(batches) == 2
    assert len(batches[0]) == 2
    assert len(batches[1]) == 2
    batches = list(ops.multibatch(2, [1, 2, 3, 4], [5, 6, 7, 8]))
    assert batches == [[[1, 2], [5, 6]], [[3, 4], [7, 8]]]
    with pytest.raises(ValueError):
        ops.multibatch(10, (i for i in range(100)), (i for i in range(100)))
    with pytest.raises(ValueError):
        ops.multibatch(10, arr1, (i for i in range(100)), arr2)


def test_ngrams():
    ops = get_current_ops()
    arr1 = numpy.asarray([1, 2, 3, 4, 5], dtype=numpy.uint64)
    for n in range(1, 10):
        assert len(ops.ngrams(n, arr1)) == max(0, arr1.shape[0] - (n - 1))
    assert len(ops.ngrams(-1, arr1)) == 0
    assert len(ops.ngrams(arr1.shape[0] + 1, arr1)) == 0


@pytest.mark.skipif(not has_torch, reason="needs PyTorch")
@pytest.mark.parametrize("ops", ALL_OPS)
@pytest.mark.parametrize("dtype", ["float32", "float64"])
@pytest.mark.parametrize("torch_func", TORCH_FUNCS)
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(x=strategies.floats(min_value=-5, max_value=5))
def test_compare_activations_to_torch(ops, dtype, x, torch_func):
    import torch

    def cast_torch(scalar: float):
        return torch.tensor([scalar], requires_grad=True)

    func_name, pytorch_func = torch_func
    forward = getattr(ops, func_name)
    backward = getattr(ops, "backprop_" + func_name)
    # The tolerance of isclose is set to 1e-06 instead of
    # the default 1e-08 due to the GELU
    x_thinc = ops.asarray([x], dtype=dtype)
    x_torch = cast_torch(x)
    y = pytorch_func(x_torch)
    y_thinc = forward(x_thinc)
    y.backward()
    assert x_thinc.dtype == y_thinc.dtype
    assert ops.xp.isclose(y_thinc, forward(x_thinc, inplace=True))
    assert ops.xp.isclose(y_thinc, y.detach().numpy(), atol=1e-06)
    x_thinc = ops.asarray([x], dtype=dtype)
    dY_thinc = ops.asarray([1.0], dtype=dtype)
    dY_thinc_inplace = dY_thinc.copy()
    if backward.__name__ == "backprop_swish":
        dx_thinc = backward(dY_thinc, Y=y_thinc, X=x_thinc)
        assert dx_thinc.dtype == x_thinc.dtype
        assert ops.xp.isclose(
            dx_thinc,
            backward(dY=dY_thinc_inplace, Y=y_thinc, X=x_thinc, inplace=True),
        )
        assert ops.xp.isclose(x_torch.grad.item(), float(dx_thinc), atol=1e-06)
    else:
        dx_thinc = backward(dY_thinc, X=x_thinc)
        assert dx_thinc.dtype == x_thinc.dtype
        assert ops.xp.isclose(
            dx_thinc, backward(dY=dY_thinc_inplace, X=x_thinc, inplace=True)
        )
        assert ops.xp.isclose(
            x_torch.grad.item(), float(backward(dY_thinc, X=x_thinc)), atol=1e-06
        )


@pytest.mark.parametrize("ops", ALL_OPS)
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(x=strategies.floats(min_value=-10, max_value=10))
def test_clipped_linear(ops, x):
    x_thinc = ops.xp.asarray([x])
    assert ops.xp.isclose(ops.clipped_linear(x_thinc, max_val=6.0), ops.relu_k(x_thinc))
    assert ops.xp.isclose(
        ops.backprop_clipped_linear(1.0, x_thinc, max_val=6.0),
        ops.backprop_relu_k(1.0, x_thinc),
    )
    assert ops.xp.isclose(
        ops.clipped_linear(x_thinc, slope=0.2, offset=0.5), ops.hard_sigmoid(x_thinc)
    )
    assert ops.xp.isclose(
        ops.backprop_clipped_linear(1.0, x_thinc, slope=0.2, offset=0.5),
        ops.backprop_hard_sigmoid(1.0, x_thinc),
    )


@pytest.mark.parametrize("ops", ALL_OPS)
@pytest.mark.parametrize("byte_order", (">", "<", "=", "|"))
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(x=strategies.floats(min_value=-10, max_value=10))
def test_to_numpy_byteorder(ops, byte_order, x):
    x = ops.xp.asarray([x])
    y = ops.to_numpy(x, byte_order=byte_order)
    assert numpy.array_equal(ops.to_numpy(x), ops.to_numpy(y))
    if byte_order in (">", "<"):
        # hack from: https://stackoverflow.com/a/49740663
        assert y.dtype.newbyteorder("S").newbyteorder("S").byteorder == byte_order
    else:
        assert x.dtype.byteorder == y.dtype.byteorder
