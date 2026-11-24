"""Exercise the read-only BLE Charge on Solar pairing core without a car or ESP32.

Byte fixtures were produced with dynamic protobuf messages built from the
descriptors in the ``tesla_protocol`` 6.3.0 wheel (Apache-2.0), which carries
Tesla's extended CarServer schema: VehicleAction.getManagedChargingSitesRequest
= 73, Response.getManagedChargingSitesResponse = 14, ManagedChargingSite
{string public_key = 1; ManagerType manager_type = 2; LatLong lat_lon = 3}.
The fixture test re-derives them from a reference schema with the same field
numbers using the protobuf runtime.
"""
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "esphome/components/tesla_ble_vehicle"

KEY_HEX = "04" + "ab" * 64
DIN = "1232100-00-E--TG123456789ABC"
GET_SITES = "1203ca0400"
RESPONSE_ONE = (
    "0a0072b6010ab3010a8201" + KEY_HEX.encode().hex()
    + "12200a1e0a1c" + DIN.encode().hex() + "1a0a0da77907c21595351743"
)
RESPONSE_EMPTY = "0a007200"
RESPONSE_ABSENT = "0a00"

SCHEMA = {
    "SiteController": [("din", 1, "string")],
    "ManagerType": [("site_controller", 1, "SiteController")],
    "LatLong": [("latitude", 1, "float"), ("longitude", 2, "float")],
    "ManagedChargingSite": [("public_key", 1, "string"), ("manager_type", 2, "ManagerType"),
                            ("lat_lon", 3, "LatLong")],
    "GetManagedChargingSitesRequest": [],
    "GetManagedChargingSitesResponse": [("sites", 1, "ManagedChargingSite", "repeated")],
    "AddManagedChargingSiteRequest": [("site", 1, "ManagedChargingSite")],
    "RemoveManagedChargingSiteRequest": [("public_key", 1, "string")],
    "ManagedChargingSiteAddedOrUpdated": [],
    "ExistingManagedChargingSiteNearby": [],
    "AddManagedChargingSiteResponse": [("added_or_updated", 1, "ManagedChargingSiteAddedOrUpdated"),
                                       ("existing_site_nearby", 2, "ExistingManagedChargingSiteNearby")],
    "VehicleAction": [("addManagedChargingSiteRequest", 67, "AddManagedChargingSiteRequest", "m"),
                      ("removeManagedChargingSiteRequest", 68, "RemoveManagedChargingSiteRequest", "m"),
                      ("getManagedChargingSitesRequest", 73, "GetManagedChargingSitesRequest", "m")],
    "Action": [("vehicleAction", 2, "VehicleAction", "m")],
    "ActionStatus": [("result", 1, "int32")],
    "Response": [("actionStatus", 1, "ActionStatus"),
                 ("getManagedChargingSitesResponse", 14, "GetManagedChargingSitesResponse", "response_msg"),
                 ("addManagedChargingSiteResponse", 16, "AddManagedChargingSiteResponse", "response_msg")],
}


def test_fixtures_match_reference_encoding(protobuf_reference):
    def encode(message, text):
        return protobuf_reference(SCHEMA, message, text)

    assert encode("Action", "vehicleAction{getManagedChargingSitesRequest{}}") == GET_SITES
    assert encode("Response", "actionStatus{} getManagedChargingSitesResponse{sites{"
                  f'public_key:"{KEY_HEX}" manager_type{{site_controller{{din:"{DIN}"}}}} '
                  "lat_lon{latitude:-33.8688 longitude:151.2093}}}") == RESPONSE_ONE
    assert encode("Response", "actionStatus{} getManagedChargingSitesResponse{}") == RESPONSE_EMPTY


def _cpp_bytes(hex_string):
    return "{" + ",".join(f"0x{hex_string[i:i + 2]}" for i in range(0, len(hex_string), 2)) + "}"


SITE_TEXT = (f'public_key:"{KEY_HEX}" manager_type{{site_controller{{din:"{DIN}"}}}} '
             "lat_lon{latitude:-33.8688 longitude:151.2093}")


def write_fixtures(encode):
    return {
        "@ADD@": _cpp_bytes(encode("Action", f"vehicleAction{{addManagedChargingSiteRequest{{site{{{SITE_TEXT}}}}}}}")),
        "@REMOVE@": _cpp_bytes(encode("Action", f'vehicleAction{{removeManagedChargingSiteRequest{{public_key:"{KEY_HEX}"}}}}')),
        "@ADDED@": _cpp_bytes(encode("Response", "actionStatus{} addManagedChargingSiteResponse{added_or_updated{}}")),
        "@NEARBY@": _cpp_bytes(encode("Response", "actionStatus{} addManagedChargingSiteResponse{existing_site_nearby{}}")),
        "@ACKED@": _cpp_bytes(encode("Response", "actionStatus{} addManagedChargingSiteResponse{}")),
    }


def test_pairing_protocol_core(tmp_path, protobuf_reference):
    source = tmp_path / "pairing_test.cpp"
    source.write_text(r'''
#include "cos_pairing.h"
#include <cassert>
#include <cstring>
#include <string>
#include <vector>
using namespace esphome::tesla_ble_vehicle;
using namespace esphome::tesla_ble_vehicle::cos_pairing;
static std::vector<uint8_t> bytes(std::initializer_list<uint8_t> b) { return b; }
static Decode decode(const std::vector<uint8_t> &b, Sites &s) { return decode_sites_response(b.data(), b.size(), s); }
static Decode decode_add_site_response_v(const std::vector<uint8_t> &b, uint8_t &o) { return decode_add_site_response(b.data(), b.size(), o); }
int main() {
  uint8_t out[16];
  const auto get = bytes(@GET@);
  assert(encode_get_sites(out, sizeof(out)) == get.size() && std::memcmp(out, get.data(), get.size()) == 0);
  assert(encode_get_sites(out, 2) == 0);

  Sites s;
  assert(decode(bytes(@ONE@), s) == Decode::Ok && s.count == 1 && s.total == 1);
  std::string key = "04";
  for (int i = 0; i < 64; ++i) key += "ab";
  assert(s.site[0].key == key);
  assert(s.site[0].din == "@DIN@" && s.site[0].has_location);
  assert(micro5(s.site[0].latitude) == -3386880 && micro5(s.site[0].longitude) == 15120930);
  assert(classify_key(s.site[0].key) == 'h');

  s = Sites{}; s.total = 9;
  assert(decode(bytes(@EMPTY@), s) == Decode::Ok && s.count == 0 && s.total == 0);  // zero sites is an answer
  assert(decode(bytes(@ABSENT@), s) == Decode::Absent);
  assert(decode(bytes({}), s) == Decode::Absent);

  // More sites than the bridge keeps: all are counted, the first four kept.
  std::vector<uint8_t> many = {0x72, 0x0c};
  for (int i = 0; i < 6; ++i) { many.push_back(0x0a); many.push_back(0x00); }
  assert(decode(many, s) == Decode::Ok && s.count == MAX_SITES && s.total == 6);

  // Malformed input never yields sites.
  auto good = bytes(@ONE@);
  for (size_t cut = 3; cut < good.size(); ++cut) {
    std::vector<uint8_t> part(good.begin(), good.begin() + cut);
    assert(decode(part, s) == Decode::Malformed);
  }
  assert(decode(bytes({0x75, 0, 0, 0, 0}), s) == Decode::Malformed);             // field 14 as fixed32
  assert(decode(bytes({0x72, 0x00, 0x72, 0x00}), s) == Decode::Malformed);       // two responses
  assert(decode(bytes({0x72, 0x04, 0x0a, 0x02, 0x08, 0x01}), s) == Decode::Malformed);  // key as varint
  assert(decode(bytes({0x72, 0x09, 0x0a, 0x07, 0x1a, 0x05, 0x0d, 0, 0, 0xc0, 0x7f}), s) == Decode::Malformed);  // NaN
  assert(decode(bytes({0x72, 0x09, 0x0a, 0x07, 0x1a, 0x05, 0x0d, 0, 0, 0xc8, 0x42}), s) == Decode::Malformed);  // lat 100
  assert(decode(bytes({0x72, 0x0a, 0x0a, 0x08, 0x12, 0x06, 0x0a, 0x04, 0x0a, 0x02, 'a', '"'}), s) == Decode::Malformed);  // quote in DIN
  // Unknown fields and other manager types are skipped.
  assert(decode(bytes({0x10, 0x05, 0x72, 0x06, 0x0a, 0x04, 0x12, 0x02, 0x12, 0x00}), s) == Decode::Ok
         && s.count == 1 && s.site[0].din.empty() && !s.site[0].has_location);

  assert(classify_key("") == 'e');
  assert(classify_key("04" + std::string(128, 'A')) == 'H');
  assert(classify_key("04" + std::string(128, 'g')) == 'o');
  assert(classify_key(std::string(86, 'B') + "==") == 'r');
  assert(classify_key(std::string(124, 'M')) == 'd');
  assert(classify_key(std::string(87, 'B') + "!") == 'o');
  assert(classify_key("=" + std::string(87, 'B')) == 'o');  // padding only at the end

  // Site writes are byte-identical to the protobuf runtime's encoding.
  uint8_t big[MAX_ACTION];
  Site add; add.key = key; add.din = "@DIN@"; add.has_location = true;
  add.latitude = float(-3386880 / 1e5); add.longitude = float(15120930 / 1e5);
  const auto add_bytes = bytes(@ADD@), remove_bytes = bytes(@REMOVE@);
  size_t n_add = encode_add_site(add, big, sizeof(big));
  assert(n_add == add_bytes.size() && std::memcmp(big, add_bytes.data(), n_add) == 0);
  size_t n_remove = encode_remove_site(key, big, sizeof(big));
  assert(n_remove == remove_bytes.size() && std::memcmp(big, remove_bytes.data(), n_remove) == 0);
  Site bad = add; bad.key = "not a key"; assert(encode_add_site(bad, big, sizeof(big)) == 0);
  bad = add; bad.din = ""; assert(encode_add_site(bad, big, sizeof(big)) == 0);
  bad = add; bad.latitude = 91; assert(encode_add_site(bad, big, sizeof(big)) == 0);
  bad = add; bad.has_location = false; assert(encode_add_site(bad, big, sizeof(big)) == 0);
  assert(encode_add_site(add, big, 16) == 0 && encode_remove_site("", big, sizeof(big)) == 0);
  uint8_t outcome = 0;
  assert(decode_add_site_response_v(bytes(@ADDED@), outcome) == Decode::Ok && outcome == 1);
  assert(decode_add_site_response_v(bytes(@NEARBY@), outcome) == Decode::Ok && outcome == 2);
  assert(decode_add_site_response_v(bytes(@ACKED@), outcome) == Decode::Ok && outcome == 3);
  assert(decode_add_site_response_v(bytes({0x0a, 0x00}), outcome) == Decode::Absent);
  assert(decode_add_site_response_v(bytes({0x82, 0x01, 0x02, 0x08, 0x01}), outcome) == Decode::Malformed);

  Request r;
  const std::string n(32, 'a');
  assert(parse_request("sit:" + n, r) && r.kind == Kind::Sites && r.nonce == n);
  assert(parse_request("key:" + n + ":v", r) && r.kind == Kind::VehicleKey && r.domain == 'v');
  assert(parse_request("key:" + n + ":i", r) && r.domain == 'i');
  assert(parse_request("ent:" + n + ":" + key, r) && r.kind == Kind::Entry && !r.write() && r.key[0] == 0x04 && r.key[64] == 0xab);
  assert(parse_request("wla:" + n + ":" + key, r) && r.kind == Kind::AddKey && r.write());
  assert(parse_request("ste:" + n + ":-3386880:15120930:@DIN@:" + key, r) && r.kind == Kind::AddSite && r.write());
  assert(r.site.din == "@DIN@" && r.site.key == key && micro5(r.site.latitude) == -3386880 && micro5(r.site.longitude) == 15120930);
  const std::string b64 = std::string(86, 'B') + "==";
  assert(parse_request("ste:" + n + ":0:0:@DIN@:" + b64, r) && r.site.key == b64);
  assert(parse_request("rms:" + n + ":" + key, r) && r.kind == Kind::RemoveSite && r.site.key == key);
  for (const std::string &bad : {"sit:" + std::string(32, 'A'), "sit:" + n + "x", "key:" + n,
       "key:" + n + ":x", "key:" + n + ":vv", "add:" + n, std::string(""), "sit" + n,
       "ent:" + n + ":" + key.substr(2), "ent:" + n + ":03" + key.substr(2), "wla:" + n + ":" + key + "0",
       "ent:" + n + ":04" + std::string(128, 'A'),
       "ste:" + n + ":9000001:0:@DIN@:" + key, "ste:" + n + ":0:18000001:@DIN@:" + key,
       "ste:" + n + ":01:0:@DIN@:" + key, "ste:" + n + ":0:0::" + key, "ste:" + n + ":0:0:@DIN@:nope",
       "ste:" + n + ":0:0:@DIN@", "ste:" + n + ":0:0:a\"b:" + key, "ste:" + n + ":0:0:@DIN@:" + key + ":x",
       "rms:" + n + ":", "rms:" + n + ":nope", "rms:" + n + ":" + key + std::string(200, 'a')})
    assert(!parse_request(bad, r));
  return 0;
}
'''.replace("@GET@", _cpp_bytes(GET_SITES)).replace("@ONE@", _cpp_bytes(RESPONSE_ONE))
       .replace("@EMPTY@", _cpp_bytes(RESPONSE_EMPTY)).replace("@ABSENT@", _cpp_bytes(RESPONSE_ABSENT))
       .replace("@DIN@", DIN))
    for marker, value in write_fixtures(lambda m, text: protobuf_reference(SCHEMA, m, text)).items():
        source.write_text(source.read_text().replace(marker, value))
    binary = tmp_path / "pairing_test"
    build = subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(COMPONENT),
                            str(source), "-o", str(binary)], capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    run = subprocess.run([str(binary)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


def test_package_wires_pairing_entities_read_only():
    package = (ROOT / "esphome/charge-on-solar.yaml").read_text()
    assert 'name: "PowerSync Charge on Solar Pairing"' in package
    assert 'name: "PowerSync Charge on Solar Pairing Request"' in package
    assert "request_cos_pairing(x)" in package
    assert package.count("restore_value: false") == 2  # a reboot never replays a request
    assert "cos_pairing_state: powersync_cos_pairing" in package
    production = (COMPONENT / "tesla_ble_vehicle.cpp").read_text()
    intercept = production.index("handle_cos_pairing_response(static_carserver_response_)")
    assert intercept < production.index("log_carserver_response(TAG, &static_carserver_response_);")
    assert production.count("remember_vehicle_key(domain,") == 2  # NVS load and fresh session
    glue = (COMPONENT / "cos_pairing.cpp").read_text()
    assert 'refusal = "recovery_locked"' in glue
    assert 'refusal = "charge_on_solar_busy"' in glue
    # No Charge on Solar setting writes, and VCSEC requests never share the queue.
    assert "encode_set" not in glue
    assert 'refusal = "command_queue_busy"' in glue
    # The key add is written exactly once and never queued for retry.
    add = glue[glue.index("buildWhitelistAddForKey"):glue.index("void TeslaBLEVehicle::enqueue_cos_pairing_carserver")]
    assert add.count("writeBLE(") == 1 and "placeAtFrontOfQueue" not in add
    assert "Keys_Role_ROLE_CHARGING_MANAGER" in add and "ROLE_OWNER" not in glue
    assert "max_length: 255" in package
    assert 'refusal = "pairing_busy"' in (COMPONENT / "charge_on_solar.cpp").read_text()
