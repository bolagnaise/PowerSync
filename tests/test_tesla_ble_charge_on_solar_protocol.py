"""Exercise the BLE Charge on Solar protocol core without a vehicle or ESP32.

Byte fixtures were produced by protoc from a schema carrying the Teslemetry
extended CarServer field numbers (Action.vehicleAction=2, VehicleAction 74/75,
Response 15, ChargeOnSolarFeature 1/2/3). When protoc is installed the fixture
test re-derives them, so the C++ encoder can never drift from the schema.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "esphome/components/tesla_ble_vehicle"

GET = "1203da0400"
SET_ON_20_80 = "1211d2040e0a0c0801150000a0411d0000a042"
SET_OFF_0_100 = "120ad204070a051d0000c842"
RESPONSE_ON_20_80 = "0a007a0e0a0c0801150000a0411d0000a042"
RESPONSE_EMPTY_FEATURE = "0a007a020a00"
RESPONSE_ABSENT = "0a00"

SCHEMA = """
syntax = "proto3";
package ref;
message ChargeOnSolarFeature { bool enabled = 1; float lower_charge_limit = 2; float upper_charge_limit = 3; }
message UpdateChargeOnSolarFeatureRequest { ChargeOnSolarFeature charge_on_solar = 1; }
message GetChargeOnSolarFeatureRequest {}
message GetChargeOnSolarFeatureResponse { ChargeOnSolarFeature charge_on_solar = 1; }
message VehicleAction { oneof m { UpdateChargeOnSolarFeatureRequest updateChargeOnSolarFeatureRequest = 74;
  GetChargeOnSolarFeatureRequest getChargeOnSolarFeatureRequest = 75; } }
message Action { oneof m { VehicleAction vehicleAction = 2; } }
message ActionStatus { int32 result = 1; }
message Response { ActionStatus actionStatus = 1;
  oneof response_msg { GetChargeOnSolarFeatureResponse getChargeOnSolarFeatureResponse = 15; } }
"""


@pytest.mark.skipif(shutil.which("protoc") is None, reason="protoc not installed")
def test_fixtures_match_protoc_reference_encoding(tmp_path):
    (tmp_path / "cos.proto").write_text(SCHEMA)

    def encode(message, text):
        result = subprocess.run(["protoc", f"--encode=ref.{message}", "cos.proto"], cwd=tmp_path,
                                input=text.encode(), capture_output=True, check=True)
        return result.stdout.hex()

    assert encode("Action", "vehicleAction{getChargeOnSolarFeatureRequest{}}") == GET
    assert encode("Action", "vehicleAction{updateChargeOnSolarFeatureRequest{charge_on_solar{"
                  "enabled:true lower_charge_limit:20 upper_charge_limit:80}}}") == SET_ON_20_80
    assert encode("Action", "vehicleAction{updateChargeOnSolarFeatureRequest{charge_on_solar{"
                  "enabled:false lower_charge_limit:0 upper_charge_limit:100}}}") == SET_OFF_0_100
    assert encode("Response", "actionStatus{} getChargeOnSolarFeatureResponse{charge_on_solar{"
                  "enabled:true lower_charge_limit:20 upper_charge_limit:80}}") == RESPONSE_ON_20_80
    assert encode("Response", "actionStatus{} getChargeOnSolarFeatureResponse{charge_on_solar{}}") == RESPONSE_EMPTY_FEATURE
    assert encode("Response", "actionStatus{}") == RESPONSE_ABSENT


def _cpp_bytes(hex_string):
    return "{" + ",".join(f"0x{hex_string[i:i + 2]}" for i in range(0, len(hex_string), 2)) + "}"


def test_protocol_core(tmp_path):
    source = tmp_path / "cos_test.cpp"
    source.write_text(r'''
#include "charge_on_solar.h"
#include <cassert>
#include <cstring>
#include <string>
#include <vector>
using namespace esphome::tesla_ble_vehicle::cos;
static std::vector<uint8_t> bytes(std::initializer_list<uint8_t> b) { return b; }
static bool same(const uint8_t *a, size_t n, const std::vector<uint8_t> &b) {
  return n == b.size() && std::memcmp(a, b.data(), n) == 0;
}
static Decode decode(const std::vector<uint8_t> &b, Settings &s) { return decode_response(b.data(), b.size(), s); }
int main() {
  uint8_t out[40];
  // Encoding is byte-identical to protoc (proto3 defaults omitted).
  assert(same(out, encode_get(out, sizeof(out)), bytes(@GET@)));
  assert(same(out, encode_set(Settings{true, 20, 80}, out, sizeof(out)), bytes(@SET_ON@)));
  assert(same(out, encode_set(Settings{false, 0, 100}, out, sizeof(out)), bytes(@SET_OFF@)));
  assert(encode_set(Settings{true, 81, 80}, out, sizeof(out)) == 0);   // lower above upper
  assert(encode_set(Settings{true, 0, 101}, out, sizeof(out)) == 0);   // above 100%
  assert(encode_set(Settings{true, 20, 80}, out, 4) == 0);             // buffer too small
  assert(encode_get(out, 2) == 0);

  Settings s;
  assert(decode(bytes(@RESP_ON@), s) == Decode::Ok && s == (Settings{true, 20, 80}));
  s = Settings{true, 9, 9};
  assert(decode(bytes(@RESP_EMPTY@), s) == Decode::Ok && s == (Settings{false, 0, 0}));
  s = Settings{true, 9, 9};
  assert(decode(bytes(@RESP_ABSENT@), s) == Decode::Absent && s == (Settings{true, 9, 9}));
  assert(decode(bytes({}), s) == Decode::Absent);
  // Unknown fields around the payload are skipped (varint, fixed64, fixed32, LEN).
  assert(decode(bytes({0x10,0x05, 0x19,1,2,3,4,5,6,7,8, 0x25,1,2,3,4, 0x7a,0x02,0x0a,0x00, 0x32,0x01,0x00}), s) == Decode::Ok);
  // Truncated / malformed input never yields settings.
  auto good = bytes(@RESP_ON@);
  for (size_t cut = 1; cut < good.size(); ++cut) {
    std::vector<uint8_t> part(good.begin(), good.begin() + cut);
    assert(decode(part, s) != Decode::Ok || cut == 2);  // cut==2 is a complete actionStatus only
  }
  assert(decode(bytes({0x7a,0x02,0x0a}), s) == Decode::Malformed);           // inner length overruns
  assert(decode(bytes({0x7d,0,0,0,0}), s) == Decode::Malformed);             // field 15 with fixed32 wire
  assert(decode(bytes({0x7a,0x04,0x0a,0x02,0x10,0x01}), s) == Decode::Malformed); // lower as varint
  assert(decode(bytes({0x7a,0x07,0x0a,0x05,0x15,0x00,0x00,0xa4,0x41}), s) == Decode::Malformed); // 20.5%
  assert(decode(bytes({0x7a,0x07,0x0a,0x05,0x1d,0x00,0x00,0xca,0x42}), s) == Decode::Malformed); // 101%
  assert(decode(bytes({0x7a,0x07,0x0a,0x05,0x1d,0x00,0x00,0xc0,0x7f}), s) == Decode::Malformed); // NaN
  assert(decode(bytes({0x7a,0x07,0x0a,0x05,0x15,0x00,0x00,0x80,0xbf}), s) == Decode::Malformed); // -1%
  assert(decode(bytes({0x03}), s) == Decode::Malformed);                     // group wire type
  assert(decode(bytes({0x80,0x80,0x80,0x80,0x80,0x80,0x80,0x80,0x80,0x80,0x01}), s) == Decode::Malformed);

  Request r;
  const std::string n(32, 'a');
  assert(parse_request("get:" + n, r) && !r.write && r.nonce == n);
  assert(parse_request("set:" + n + ":1:20:80", r) && r.write && r.settings == (Settings{true, 20, 80}));
  assert(parse_request("set:" + n + ":0:0:100", r) && r.settings == (Settings{false, 0, 100}));
  for (const std::string &bad : {"get:" + std::string(32, 'A'), "get:" + n + "x", "get:" + n.substr(1),
       "set:" + n, "set:" + n + ":2:20:80", "set:" + n + ":1:80:20", "set:" + n + ":1:20:101",
       "set:" + n + ":1:020:80", "set:" + n + ":1:-1:80", "set:" + n + ":1:20", "set:" + n + ":1:20:80:1",
       "put:" + n, std::string(""), "get" + n})
    assert(!parse_request(bad, r));

  // State machine: one request at a time, no replays, exact correlation.
  Protocol p;
  const std::string u1(32, '1'), u2(32, '2'), u3(32, '3');
  const uint8_t h[17] = {9};
  Request read; assert(parse_request("get:" + n, read));
  assert(p.begin(read, 1000) && p.busy() && p.reading_back && p.phase == 'w');
  assert(!p.begin(read, 1000));
  Request other; assert(parse_request("get:" + std::string(32, 'b'), other));
  assert(!p.begin(other, 1000) && !p.refuse(other, "busy"));  // in-flight request not displaced
  assert(p.record(u1.c_str(), h, 17) && !p.write_sent);
  assert(!p.record("short", h, 17));
  assert(p.find(u1) != nullptr && p.find(u1)->hash_len == 17 && p.find(u2) == nullptr);
  p.fail("timeout");
  assert(p.phase == 'f' && !p.busy() && p.find(u1) == nullptr);   // a failed read changed nothing
  assert(!p.begin(read, 2000));                                   // same nonce cannot replay
  assert(p.refuse(other, "recovery_locked") && p.phase == 'f' && p.nonce == other.nonce);

  Request write; assert(parse_request("set:" + std::string(32, 'c') + ":1:20:80", write));
  assert(p.begin(write, 5000) && !p.reading_back);
  assert(!p.expired(5000 + Protocol::TIMEOUT_MS - 1) && p.expired(5000 + Protocol::TIMEOUT_MS));
  assert(p.record(u1.c_str(), h, 17) && p.write_sent);
  p.fail("write_rejected", true);
  assert(p.phase == 'f');                       // one frame, explicitly rejected: not applied

  Request write2; assert(parse_request("set:" + std::string(32, 'd') + ":1:20:80", write2));
  assert(p.begin(write2, 9000));
  assert(p.record(u1.c_str(), h, 17) && p.record(u2.c_str(), h, 17));  // an unanswered retry
  p.fail("write_rejected", true);
  assert(p.phase == 'u');                       // the first frame may still have been applied

  Request write3; assert(parse_request("set:" + std::string(32, 'e') + ":0:0:100", write3));
  assert(p.begin(write3, 9000) && p.record(u1.c_str(), h, 17));
  p.fail("timeout");
  assert(p.phase == 'u');                       // sent but unanswered

  Request write4; assert(parse_request("set:" + std::string(32, 'f') + ":1:20:80", write4));
  assert(p.begin(write4, 9000) && p.record(u1.c_str(), h, 17));
  p.acknowledged();
  assert(p.busy() && p.reading_back && p.write_acked && p.find(u1) == nullptr);  // readback uses new UUIDs
  assert(p.record(u3.c_str(), h, 17) && p.find(u3) != nullptr);
  p.fail("read_rejected", true);
  assert(p.phase == 'u');                       // acked write, unconfirmed readback

  Request write5; assert(parse_request("set:" + std::string(31, 'f') + "0:1:20:80", write5));
  assert(p.begin(write5, 9000) && p.record(u1.c_str(), h, 17));
  p.acknowledged();
  p.confirmed(Settings{true, 20, 80});
  assert(p.phase == 'k' && !p.busy() && p.observed == (Settings{true, 20, 80}));

  // The UUID ring keeps the most recent MAX_SENT frames.
  Request ring; assert(parse_request("get:" + std::string(31, 'e') + "0", ring));
  assert(p.begin(ring, 0));
  for (int i = 0; i < 10; ++i) {
    char uuid[33]; std::snprintf(uuid, sizeof(uuid), "%032x", i);
    assert(p.record(uuid, h, 17));
  }
  char first[33], last[33];
  std::snprintf(first, sizeof(first), "%032x", 0); std::snprintf(last, sizeof(last), "%032x", 9);
  assert(p.find(first) == nullptr && p.find(last) != nullptr);
  return 0;
}
'''.replace("@GET@", _cpp_bytes(GET)).replace("@SET_ON@", _cpp_bytes(SET_ON_20_80))
       .replace("@SET_OFF@", _cpp_bytes(SET_OFF_0_100)).replace("@RESP_ON@", _cpp_bytes(RESPONSE_ON_20_80))
       .replace("@RESP_EMPTY@", _cpp_bytes(RESPONSE_EMPTY_FEATURE)).replace("@RESP_ABSENT@", _cpp_bytes(RESPONSE_ABSENT)))
    binary = tmp_path / "cos_test"
    build = subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(COMPONENT),
                            str(source), "-o", str(binary)], capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    run = subprocess.run([str(binary)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


def test_package_wires_request_and_state_to_the_component():
    package = (ROOT / "esphome/charge-on-solar.yaml").read_text()
    assert 'name: "PowerSync Charge on Solar"' in package
    assert 'name: "PowerSync Charge on Solar Request"' in package
    assert "request_charge_on_solar(x)" in package
    assert "restore_value: false" in package  # a reboot must never replay a request
    assert "charge_on_solar_state: powersync_charge_on_solar" in package
    production = (COMPONENT / "tesla_ble_vehicle.cpp").read_text()
    # The correlated reply is consumed before the generic retry/poll handling.
    intercept = production.index("handle_charge_on_solar_response(static_carserver_response_)")
    assert intercept < production.index("log_carserver_response(TAG, &static_carserver_response_);")
    assert "new PowerSyncBLEClient{}" in production
    glue = (COMPONENT / "charge_on_solar.cpp").read_text()
    assert 'refusal = "recovery_locked"' in glue  # recovery lock refuses every request
