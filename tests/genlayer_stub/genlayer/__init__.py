"""
Minimal offline stub of the `genlayer` SDK.

THIS IS A TEST-ONLY SHIM, NOT PART OF THE DEPLOYABLE CONTRACT.

The real SDK (`genlayer` package and the GenVM runtime) only exists inside a
GenLayer node or GenLayer Studio. This stub reproduces just enough of its
surface to import and exercise `contract.py` in plain Python:

  * storage types (TreeMap, DynArray, u256, Address) behave like dict, list, int
  * `gl.public.*` decorators are no-ops
  * `gl.message` carries a settable sender and value
  * `gl.evm` records every `emit_transfer` in a ledger
  * `gl.nondet.web.request`, `gl.nondet.web.render` and `gl.nondet.exec_prompt`
    raise by default and are patched per test
  * `gl.vm.run_nondet_unsafe` runs the leader once and the validator once,
    exactly like a single-validator network

It deliberately does NOT simulate real networking, real model behaviour,
multi-validator consensus or GenVM's pinned clock. Those need Studio.
"""

__all__ = ["gl", "TreeMap", "u256", "DynArray", "i256", "bigint", "Address"]


class _SubscriptableContainer:
    def __class_getitem__(cls, item):
        return cls


class TreeMap(_SubscriptableContainer, dict):
    """Stand-in for a persistent TreeMap."""


class DynArray(_SubscriptableContainer, list):
    """Stand-in for a persistent DynArray."""


class u256(int):
    """Stand-in for a fixed-width unsigned integer."""


class i256(int):
    """Stand-in for a fixed-width signed integer."""


class bigint(int):
    """Stand-in for an arbitrary-precision integer."""


class Address:
    """Validates a 0x-prefixed 20-byte hex string; compares case-insensitively."""

    def __init__(self, value):
        text = str(value)
        if not text.startswith("0x") or len(text) != 42:
            raise ValueError(f"invalid address: {text!r}")
        int(text[2:], 16)
        self._value = text

    def __str__(self):
        return self._value

    def __repr__(self):
        return f"Address({self._value!r})"

    def __eq__(self, other):
        return str(self).lower() == str(other).lower()

    def __hash__(self):
        return hash(str(self).lower())


class UserError(Exception):
    """Stand-in for gl.vm.UserError."""


class _Vm:
    UserError = UserError

    class Return:
        def __init__(self, calldata):
            self.calldata = calldata

    @staticmethod
    def run_nondet_unsafe(leader_fn, validator_fn):
        result = leader_fn()
        verdict = validator_fn(_Vm.Return(result))
        if not verdict:
            raise UserError("validator rejected leader result")
        return result


class _WriteDecorator:
    def __call__(self, fn):
        return fn

    @staticmethod
    def payable(fn):
        return fn


class _PublicNamespace:
    write = _WriteDecorator()

    @staticmethod
    def view(fn):
        return fn


class _Response:
    """What `gl.nondet.web.request` returns: `status_code` and a bytes `body`."""

    def __init__(self, status_code, body=b""):
        self.status_code = status_code
        self.body = body if isinstance(body, (bytes, bytearray)) else str(body).encode("utf-8")


class _NondetWeb:
    Response = _Response

    @staticmethod
    def request(url, method="GET", **kwargs):
        raise NotImplementedError("gl.nondet.web.request must be patched in tests")

    @staticmethod
    def render(url, mode="text"):
        raise NotImplementedError("gl.nondet.web.render must be patched in tests")


class _Nondet:
    web = _NondetWeb()

    @staticmethod
    def exec_prompt(prompt, response_format="text"):
        raise NotImplementedError("gl.nondet.exec_prompt must be patched in tests")


class _EqPrinciple:
    @staticmethod
    def strict_eq(fn):
        return fn()


class _Message:
    sender_address = None
    value = u256(10 ** 18)


class _Contract:
    """Pre-populates TreeMap/DynArray class annotations with empty instances,
    like the real runtime, and gives each instance a `balance`."""

    def __new__(cls, *args, **kwargs):
        instance = super().__new__(cls)
        for klass in reversed(cls.__mro__):
            for name, annotation in vars(klass).get("__annotations__", {}).items():
                if isinstance(annotation, type) and issubclass(annotation, (TreeMap, DynArray)):
                    setattr(instance, name, annotation())
        instance.balance = u256(0)
        return instance

    def __init__(self, *args, **kwargs):
        pass


class _EvmContractProxy:
    def __init__(self, address):
        self.address = address

    def emit_transfer(self, value):
        _EvmNamespace.transfers.append({"to": str(self.address), "value": u256(value)})


class _EvmNamespace:
    transfers = []

    @staticmethod
    def contract_interface(cls):
        return _EvmContractProxy


class _GL:
    Contract = _Contract
    public = _PublicNamespace()
    nondet = _Nondet()
    eq_principle = _EqPrinciple()
    vm = _Vm()
    message = _Message()
    evm = _EvmNamespace()


gl = _GL()
