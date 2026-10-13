"""Shared test bootstrap: wires up the offline genlayer SDK stub, loads the
contract once (contract.py by default, or the file named by SS_CONTRACT) and
provides builders for PyPI payloads and time helpers."""
import datetime as _dt
import hashlib as _h
import importlib.util
import json as _json
import os
import sys
from unittest.mock import patch as _patch

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_STUB_DIR = os.path.join(_THIS_DIR, "genlayer_stub")
if _STUB_DIR not in sys.path:
    sys.path.insert(0, _STUB_DIR)

CONTRACT_PATH = os.environ.get("SS_CONTRACT") or os.path.join(os.path.dirname(_THIS_DIR), "contract.py")
_spec = importlib.util.spec_from_file_location("shipseal_contract", CONTRACT_PATH)
_contract_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_contract_module)

m = _contract_module
ShipSeal = m.ShipSeal
gl = m.gl
Address = m.Address
u256 = m.u256

ShipSeal.MIN_STAKE = u256(1)
ShipSeal.MIN_JUROR_STAKE = u256(1)
ShipSeal.MIN_APPEAL_BOND = u256(1)

CLIENT = "0x" + "11" * 20
DEV = "0x" + "22" * 20
STRANGER = "0x" + "33" * 20
JUROR_ADDRESSES = ["0x" + hex(0xa0 + i)[2:].rjust(2, "0") * 20 for i in range(12)]

REPO = "https://github.com/acme/widget"
SHA_WHEEL = "a" * 64
SHA_SDIST = "b" * 64
EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)


def make_contract():
    return ShipSeal()


def set_caller(address_str):
    gl.message.sender_address = Address(address_str)


def reset_transfers():
    gl.evm.transfers.clear()


def call_payable(contract, method_name, value, *args, **kwargs):
    gl.message.value = u256(value)
    contract.balance = contract.balance + u256(value)
    try:
        return getattr(contract, method_name)(*args, **kwargs)
    except Exception:
        contract.balance = contract.balance - u256(value)
        raise
    finally:
        gl.message.value = u256(10 ** 18)


def now():
    return _dt.datetime.now(_dt.timezone.utc)


def iso(offset_seconds=0):
    return (now() + _dt.timedelta(seconds=offset_seconds)).isoformat()


def stamp(offset_seconds=0):
    """PyPI-style upload_time_iso_8601 (UTC, microseconds, trailing Z)."""
    return (now() + _dt.timedelta(seconds=offset_seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def release_payload(package="widget", version="1.0.0", *, files=None, yanked=False, repo=REPO,
                    summary="A widget library", description="Widgets with a command line interface",
                    requires_python=">=3.9", name=None, ver=None, project_urls=None, home_page=None):
    """A PyPI /pypi/<pkg>/<ver>/json document. `files` is a list of
    (packagetype, upload offset seconds, sha256) tuples; default is one
    wheel and one sdist uploaded now."""
    if files is None:
        files = [("bdist_wheel", 0, SHA_WHEEL), ("sdist", 0, SHA_SDIST)]
    urls = []
    for kind, offset, sha in files:
        urls.append({
            "packagetype": kind,
            "filename": f"{package}-{version}.{'whl' if kind == 'bdist_wheel' else 'tar.gz'}",
            "digests": {"sha256": sha, "md5": "x"},
            "upload_time_iso_8601": stamp(offset),
            "yanked": yanked,
            "downloads": -1,
        })
    urls_map = project_urls if project_urls is not None else {"Source": repo}
    return {
        "info": {
            "name": name or package, "version": ver or version, "yanked": yanked,
            "summary": summary, "description": description, "requires_python": requires_python,
            "project_urls": urls_map, "home_page": home_page,
            "requires_dist": ["requests>=2"], "classifiers": ["Programming Language :: Python :: 3"],
            "keywords": "widget",
        },
        "urls": urls,
        "last_serial": 123,
    }


def index_payload(package="widget", versions=("0.1.0",), name=None):
    return {"info": {"name": name or package}, "releases": {v: [] for v in versions}}


class FakePyPI:
    """Routes gl.nondet.web.request by URL. `routes` maps URL -> (status, body)."""

    def __init__(self, package="widget", version="1.0.0"):
        self.package = package
        self.version = version
        self.routes = {}
        self.calls = []

    @property
    def release_url(self):
        return f"https://pypi.org/pypi/{self.package}/{self.version}/json"

    @property
    def index_url(self):
        return f"https://pypi.org/pypi/{self.package}/json"

    def release(self, payload=None, status=200):
        body = payload if isinstance(payload, str) else _json.dumps(payload if payload is not None else release_payload(self.package, self.version))
        self.routes[self.release_url] = (status, body)
        return self

    def missing(self):
        self.routes[self.release_url] = (404, "Not Found")
        return self

    def index(self, payload=None, status=200):
        body = payload if isinstance(payload, str) else _json.dumps(payload if payload is not None else index_payload(self.package))
        self.routes[self.index_url] = (status, body)
        return self

    def request(self, url, method="GET", **kwargs):
        self.calls.append(url)
        if url not in self.routes:
            return gl.nondet.web.Response(404, b"Not Found")
        status, body = self.routes[url]
        if status is None:
            raise RuntimeError("simulated network failure")
        return gl.nondet.web.Response(status, body)

    def patch(self):
        return _patch.object(gl.nondet.web, "request", side_effect=self.request)


def model(outcome="DELIVERED", reasoning="criteria shown in metadata"):
    """Patch for gl.nondet.exec_prompt returning a fixed model answer."""
    return _patch.object(gl.nondet, "exec_prompt", return_value={"outcome": outcome, "reasoning": reasoning})


def create_deal(contract, *, caller=CLIENT, value=1000, party_b=DEV, scope="Ship widget 1.0.0",
                criteria="Package exposes a command line interface", package="widget", version="1.0.0",
                sha="", kinds="wheel,sdist", repo=REPO, deadline_offset=7200):
    set_caller(caller)
    return call_payable(contract, "create_deal", value, party_b, scope, criteria, package, version,
                        sha, kinds, repo, iso(deadline_offset))


def open_deal(contract, **kwargs):
    deal_id = create_deal(contract, **kwargs)
    set_caller(kwargs.get("party_b", DEV))
    call_payable(contract, "accept_deal", kwargs.get("value", 1000), deal_id)
    return deal_id


def load(contract, deal_id):
    return _json.loads(contract.get_deal(deal_id))


def store(contract, deal_id, deal):
    contract.deals[deal_id] = _json.dumps(deal, sort_keys=True)


def patch_deal(contract, deal_id, **fields):
    deal = load(contract, deal_id)
    deal.update(fields)
    store(contract, deal_id, deal)
    return deal


def pass_deadline(contract, deal_id, seconds_past=10):
    """Move created_at/accepted_at/deadline into the past so uploads made 'now'
    (offset 0) are after the deadline unless given a negative offset."""
    deal = load(contract, deal_id)
    deal["deadline"] = iso(-seconds_past)
    deal["created_at"] = iso(-seconds_past - 7200)
    deal["accepted_at"] = iso(-seconds_past - 7000)
    store(contract, deal_id, deal)


def pass_grace(contract, deal_id):
    pass_deadline(contract, deal_id, seconds_past=m.ABSENCE_GRACE_SECONDS + 60)


def resolve(contract, deal_id, fake, answer="DELIVERED", caller=STRANGER):
    set_caller(caller)
    with fake.patch(), model(answer):
        return _json.loads(contract.resolve_tier1(deal_id))


def deliver_early(contract, deal_id=None, **kw):
    """Open a deal and resolve it as DELIVERED before the deadline."""
    deal_id = deal_id or open_deal(contract, **kw)
    fake = FakePyPI().release().index()
    return deal_id, resolve(contract, deal_id, fake)


def age_jurors(contract):
    for key in list(contract.jurors.keys()):
        rec = _json.loads(contract.jurors[key])
        rec["registered_at"] = "2000-01-01T00:00:00+00:00"
        rec["stake_updated_at"] = "2000-01-01T00:00:00+00:00"
        contract.jurors[key] = _json.dumps(rec, sort_keys=True)


def draw_jury(contract, deal_id, beacon="fixed-test-beacon"):
    deal = load(contract, deal_id)
    t = now()
    deal["appeal_deadline"] = (t - _dt.timedelta(seconds=300)).isoformat()
    deal["appeal"]["draw_round"] = int((t.timestamp() - 240 - 1595431050) // 30 + 1)
    store(contract, deal_id, deal)
    with _patch.object(gl.nondet.web, "render",
                       return_value=_json.dumps({"round": deal["appeal"]["draw_round"], "randomness": beacon})):
        contract.draw_jury(deal_id)


def commit_hash(vote, salt, juror, deal_id):
    return _h.sha256(f"{vote}:{salt}:{juror.lower()}:{deal_id}".encode()).hexdigest()
