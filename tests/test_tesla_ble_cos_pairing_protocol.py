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
    "VehicleAction": [("getManagedChargingSitesRequest", 73, "GetManagedChargingSitesRequest", "m")],
    "Action": [("vehicleAction", 2, "VehicleAction", "m")],
    "ActionStatus": [("result", 1, "int32")],
    "Response": [("actionStatus", 1, "ActionStatus"),
                 ("getManagedChargingSitesResponse", 14, "GetManagedChargingSitesResponse", "response_msg")],
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


def test_pairing_protocol_core(tmp_path):
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

  Request r;
  const std::string n(32, 'a');
  assert(parse_request("sit:" + n, r) && r.kind == Kind::Sites && r.nonce == n);
  assert(parse_request("key:" + n + ":v", r) && r.kind == Kind::VehicleKey && r.domain == 'v');
  assert(parse_request("key:" + n + ":i", r) && r.domain == 'i');
  for (const std::string &bad : {"sit:" + std::string(32, 'A'), "sit:" + n + "x", "key:" + n,
       "key:" + n + ":x", "key:" + n + ":vv", "add:" + n, std::string(""), "sit" + n})
    assert(!parse_request(bad, r));
  return 0;
}
'''.replace("@GET@", _cpp_bytes(GET_SITES)).replace("@ONE@", _cpp_bytes(RESPONSE_ONE))
       .replace("@EMPTY@", _cpp_bytes(RESPONSE_EMPTY)).replace("@ABSENT@", _cpp_bytes(RESPONSE_ABSENT))
       .replace("@DIN@", DIN))
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
    # Only the read action is ever encoded: no whitelist, site or setting writes.
    assert "encode_get_sites" in glue and "encode_set" not in glue and "WhiteList" not in glue
    assert 'refusal = "pairing_busy"' in (COMPONENT / "charge_on_solar.cpp").read_text()
