"""Exercise the firmware's real protocol core without a vehicle or ESP32."""
from pathlib import Path
import subprocess

import pytest


def test_persisted_reboot_and_native_response_correlation(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "stop_recovery_test.cpp"
    source.write_text(r'''
#include "stop_recovery.h"
#include <cassert>
#include <cmath>
using namespace esphome::tesla_ble_vehicle;
int main() {
  const uint8_t frame[] = {0, 3, 8, 2, 1};
  const uint8_t *payload = nullptr;
  size_t payload_length = 0;
  assert(recovery_payload(frame, sizeof(frame), payload, payload_length));
  assert(payload == frame + 2 && payload_length == 3 && payload[0] == 8);
  assert(!recovery_payload(frame, sizeof(frame)-1, payload, payload_length));
  assert(!recovery_payload(frame, 1, payload, payload_length));
  StopRecoveryProtocol p;
  StopRecoveryStore empty;
  p.load(empty, "1111111111111111");
  const std::string nonce(32, 'a'), uuid1(32, 'b'), uuid2(32, 'c');
  assert(!p.locked());
  assert(!p.prepare("invalid"));
  assert(p.prepare(nonce));
  assert(p.locked() && !p.reading() && p.phase == 'p');
  assert(!p.prepare(nonce)); // duplicate request cannot advance generation
  assert(!p.prepare(std::string(32, 'd'))); // pending reboot cannot be replaced
  assert(!p.sample(uuid1, 1000, true, 'S', 0, 0));
  assert(!p.can_release(nonce, 1000));
  auto persisted = p.saved;
  p = StopRecoveryProtocol{};
  p.load(persisted, "2222222222222222");
  assert(p.reading() && p.saved.generation == 1);
  assert(p.expect(uuid1));
  assert(!p.sample(uuid2, 1000, true, 'S', 0, 0)); // unrelated response
  assert(p.sequence == 0);
  assert(p.sample(uuid1, 1000, true, 'S', 0, 0));
  assert(!p.sample(uuid1, 2000, true, 'S', 0, 0)); // duplicate response
  assert(!p.can_release(nonce, 2000));
  assert(p.expect(uuid2));
  assert(p.sample(uuid2, 1500, true, 'S', 0, 0)); // too soon
  assert(p.zero_samples == 1);
  assert(p.expect(uuid1));
  assert(p.sample(uuid1, 2500, true, 'S', 0, 0));
  assert(p.can_release(nonce, 2500));
  assert(!p.can_release(std::string(32, 'd'), 2500));
  assert(!p.can_release(nonce, 7501)); // stale sample
  for (int bad = 0; bad < 7; ++bad) {
    assert(p.expect(uuid1));
    assert(p.sample(uuid1, 8500+bad*1000, bad != 0,
        bad == 1 ? '?' : 'S', bad == 2 ? 1 : bad == 3 ? NAN : bad == 4 ? -1 : 0,
        bad == 5 ? 1 : bad == 6 ? NAN : 0));
    assert(!p.can_release(nonce, 8500+bad*1000));
  }
  assert(p.expect(uuid1)); assert(p.sample(uuid1, 20000, true, 'D', 0, 0));
  assert(p.expect(uuid2)); assert(p.sample(uuid2, 21000, true, 'D', 0, 0));
  assert(p.can_release(nonce, 21000));
  p.released();
  assert(!p.locked());
  assert(!p.prepare(nonce)); // late replay cannot re-lock a completed generation
  p.held();
  assert(p.locked() && !p.reading() && !p.can_release(nonce, 21000));
  // A fresh explicit retry uses a new nonce and reboot, never old samples.
  assert(p.prepare(std::string(32, 'd')));
  assert(p.saved.generation == 2 && p.sequence == 0 && p.zero_samples == 0);
  StopRecoveryProtocol same_boot;
  same_boot.load(p.saved, p.boot);
  assert(same_boot.phase == 'h');
  StopRecoveryProtocol corrupted;
  empty.magic = 0;
  corrupted.load(empty, "3333333333333333");
  assert(corrupted.locked() && corrupted.phase == 'h');
  p.saved.generation = UINT32_MAX;
  p.held();
  assert(!p.prepare(std::string(32, 'e'))); // no generation wrap
}
''')
    binary = tmp_path / "stop_recovery_test"
    subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I",
        str(root / "esphome/components/tesla_ble_vehicle"), str(source), "-o", str(binary)],
        check=True, capture_output=True, text=True)
    subprocess.run([str(binary)], check=True, capture_output=True, text=True)


@pytest.fixture(scope="module")
def recovery_sleep_binary(tmp_path_factory):
    """Compile actual production queue admission and the real proof protocol.

    Execute queue admission and auth/wake transitions with mocked authenticated
    peers/radio responses, then require real correlated native proof to release.
    """
    root = Path(__file__).resolve().parents[1]
    out = tmp_path_factory.mktemp("ble_recovery_sleep")
    production = (root / "esphome/components/tesla_ble_vehicle/tesla_ble_vehicle.cpp").read_text()
    start = production.index("      case BLECommandState::IDLE:", production.index("void TeslaBLEVehicle::process_command_queue()"))
    end = production.index("      case BLECommandState::WAITING_FOR_VCSEC_AUTH:", start)
    (out / "actual_idle_case.inc").write_text(production[start:end])
    start = production.index("      if (recovery_proof_ && stop_recovery_.locked()", production.index("int TeslaBLEVehicle::sendCarServerVehicleActionMessage("))
    end = production.index("      if (get_action_detail(action)", start)
    (out / "actual_action_admission.inc").write_text(production[start:end])
    start = production.index("      case BLECommandState::WAITING_FOR_INFOTAINMENT_AUTH:")
    end = production.index("      case BLECommandState::WAITING_FOR_WAKE:", start)
    (out / "actual_auth_case.inc").write_text(production[start:end])
    start = production.index("      case BLECommandState::WAITING_FOR_WAKE:")
    end = production.index("      case BLECommandState::WAITING_FOR_LOCK_RESPONSE:", start)
    (out / "actual_wake_cases.inc").write_text(production[start:end])
    start = production.index("      if (recovery_proof_ && stop_recovery_.locked()", production.index("int TeslaBLEVehicle::sendVCSECActionMessage("))
    end = production.index("      ESP_LOGD", start)
    (out / "actual_wake_admission.inc").write_text(production[start:end])
    source = out / "sleep_recovery.cpp"
    source.write_text(r'''
#include "stop_recovery.h"
#include <cassert>
#include <cstring>
#include <iostream>
#include <queue>
#include <stdexcept>
#include <string>
using namespace esphome::tesla_ble_vehicle;
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGD(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
constexpr int UniversalMessage_Domain_DOMAIN_BROADCAST=0;
constexpr int UniversalMessage_Domain_DOMAIN_VEHICLE_SECURITY=1;
constexpr int UniversalMessage_Domain_DOMAIN_INFOTAINMENT=2;
constexpr int VCSEC_RKEAction_E_RKE_ACTION_WAKE_VEHICLE=1;
constexpr unsigned MAX_LATENCY=1000, MAX_RETRIES=3;
enum class BLE_CarServer_VehicleAction { GET_CHARGE_STATE, GET_CLIMATE_STATE, SET_CHARGING_SWITCH };
enum class BLECommandState { IDLE, READY, WAITING_FOR_VCSEC_AUTH, WAITING_FOR_INFOTAINMENT_AUTH,
 WAITING_FOR_INFOTAINMENT_AUTH_RESPONSE, WAITING_FOR_WAKE, WAITING_FOR_WAKE_RESPONSE };
enum class BinarySensorId { IsAsleep };
struct BinarySensor { bool state{true}; bool known{true}; bool has_state() const { return known; } };
struct BLECommand {
 int domain=UniversalMessage_Domain_DOMAIN_INFOTAINMENT;
 std::string execute_name="getChargeState";
 BLE_CarServer_VehicleAction action=BLE_CarServer_VehicleAction::GET_CHARGE_STATE;
 BLECommandState state=BLECommandState::IDLE;
 unsigned started_at=0, last_tx_at=0, retry_count=0;
};
struct Peer { bool isInitialized() const { return true; } };
struct Client { Peer peer; Peer *getPeer(int) { return &peer; } };
struct FakeQueue {
 StopRecoveryProtocol stop_recovery_;
 bool recovery_proof_{true};
 BinarySensor asleep;
 BinarySensor *binary_sensors_[1]{&asleep};
 std::queue<BLECommand> command_queue_;
 Client client;
 Client *tesla_ble_client_=&client;
 unsigned clock=0, wake_calls=0;
 bool vcsec_session=true, wake_acknowledged=true;
 void pop_command_and_tidy_up() { command_queue_.pop(); }
 int sendVCSECActionMessage(int action) {
#include "actual_wake_admission.inc"
  if(!vcsec_session) return -1;
  ++wake_calls;
  if(wake_acknowledged) { asleep.state=false; asleep.known=true; }
  return 0;
 }
 int sendVCSECInformationRequest() { return 0; }
 int sendSessionInfoRequest(int) { return 0; }
 int admit(BLE_CarServer_VehicleAction action, const std::string &name) {
#include "actual_action_admission.inc"
  BLECommand command;
  command.action=action;
  command.execute_name=name;
  command_queue_.push(command);
  return 0;
 }
 void process_once() {
  if(command_queue_.empty()) return;
  BLECommand current_command=command_queue_.front();
  unsigned now=(clock+=2000);
  switch(current_command.state) {
#include "actual_idle_case.inc"
#include "actual_auth_case.inc"
#include "actual_wake_cases.inc"
   default: break;
  }
  command_queue_.front()=current_command;
 }
 void read_only() {
  stop_recovery_.load(StopRecoveryStore{}, "1111111111111111");
  assert(stop_recovery_.prepare(std::string(32,'a')));
  auto persisted=stop_recovery_.saved;
  stop_recovery_=StopRecoveryProtocol{};
  stop_recovery_.load(persisted, "2222222222222222");
  assert(stop_recovery_.reading());
 }
};
void require(bool value, const char *message) {
 if(!value) throw std::runtime_error(message);
}
void terminal_samples(StopRecoveryProtocol &p) {
 const std::string query1(32,'b'), query2(32,'c');
 require(p.expect(query1), "first fresh query admitted");
 require(p.sample(query1,10000,true,'S',0,0), "first native zero sampled");
 require(!p.can_release(std::string(32,'a'),10000), "one sample cannot release");
 require(p.expect(query2), "second fresh query admitted");
 require(p.sample(query2,11500,true,'S',0,0), "second native zero sampled");
 require(p.can_release(std::string(32,'a'),11500), "two fresh native zero samples permit terminal release");
 p.released();
 require(!p.locked() && p.phase=='r', "terminal released receipt");
}
int main(int argc, char **argv) {
 const std::string scenario=argc>1 ? argv[1] : "sleep_recovery";
 try {
  FakeQueue f;
  f.read_only();
  if(scenario=="sleep_recovery" || scenario=="awake_recovery" || scenario=="unknown_after_reboot") {
   if(scenario=="awake_recovery") f.asleep.state=false;
   if(scenario=="unknown_after_reboot") { f.asleep.state=false; f.asleep.known=false; }
   require(f.admit(BLE_CarServer_VehicleAction::GET_CHARGE_STATE,"getChargeState")==0,"recovery charge query admitted");
   f.process_once();
   require(!f.command_queue_.empty(),"BUG: actual production IDLE drops recovery GET_CHARGE_STATE while asleep");
   require(f.command_queue_.front().state==BLECommandState::WAITING_FOR_INFOTAINMENT_AUTH,"recovery query must reach auth/wake state machine");
   for(int step=0; step<20 && !f.command_queue_.empty()
       && f.command_queue_.front().state!=BLECommandState::READY; ++step) f.process_once();
   require(!f.command_queue_.empty() && f.command_queue_.front().state==BLECommandState::READY,
       "actual auth/wake transitions must reach a ready charge-state query");
   require(f.wake_calls==(scenario=="awake_recovery" ? 0u : 1u),"sleeping or unknown recovery vehicle needs bounded wake acquisition");
   terminal_samples(f.stop_recovery_);
  } else if(scenario=="wake_unavailable" || scenario=="missing_vcsec_session" || scenario=="unknown_wake_unavailable") {
   if(scenario=="unknown_wake_unavailable") { f.asleep.state=false; f.asleep.known=false; f.wake_acknowledged=false; }
   if(scenario=="wake_unavailable") f.wake_acknowledged=false;
   else if(scenario=="missing_vcsec_session") f.vcsec_session=false;
   require(f.admit(BLE_CarServer_VehicleAction::GET_CHARGE_STATE,"getChargeState")==0,"query admitted");
   for(int step=0; step<20 && !f.command_queue_.empty(); ++step) f.process_once();
   require(f.command_queue_.empty(),"bounded failed wake must end the query");
   require(f.stop_recovery_.locked() && !f.stop_recovery_.can_release(std::string(32,'a'),50000),
       "no native proof must retain the recovery lock");
  } else if(scenario=="normal_background") {
   f.stop_recovery_=StopRecoveryProtocol{};
   require(f.admit(BLE_CarServer_VehicleAction::GET_CHARGE_STATE,"getChargeState")==0,"background charge query admitted");
   f.process_once();
   require(f.command_queue_.empty(),"ordinary sleeping poll must still drop");
  } else if(scenario=="no_companion") {
   f.stop_recovery_=StopRecoveryProtocol{};
   f.recovery_proof_=false;
   require(f.admit(BLE_CarServer_VehicleAction::GET_CHARGE_STATE,"getChargeState")==0,"ordinary query admitted");
   f.process_once();
   require(f.command_queue_.empty(),"sleeping poll without companion must still drop");
  } else if(scenario=="write_rejected") {
   require(f.admit(BLE_CarServer_VehicleAction::SET_CHARGING_SWITCH,"setChargingSwitch")==-1,"recovery rejects charge mutation");
   require(f.command_queue_.empty(),"mutation cannot create queue work");
  } else if(scenario=="held") {
   f.stop_recovery_.held();
   require(f.sendVCSECActionMessage(VCSEC_RKEAction_E_RKE_ACTION_WAKE_VEHICLE)==-1,"held mode blocks wake");
   require(f.admit(BLE_CarServer_VehicleAction::GET_CHARGE_STATE,"getChargeState")==-1,"held mode must reject charge query");
   require(f.command_queue_.empty(),"held mode cannot create queue work");
  } else if(scenario=="pending") {
   require(f.stop_recovery_.prepare(std::string(32,'d')),"retry prepare pending reboot");
   require(f.sendVCSECActionMessage(VCSEC_RKEAction_E_RKE_ACTION_WAKE_VEHICLE)==-1,"pending mode blocks wake");
   require(f.admit(BLE_CarServer_VehicleAction::GET_CHARGE_STATE,"getChargeState")==-1,"pending reboot must reject charge query");
   require(f.command_queue_.empty(),"pending mode cannot create queue work");
  } else if(scenario=="unrelated_read") {
   require(f.admit(BLE_CarServer_VehicleAction::GET_CLIMATE_STATE,"getClimateState")==-1,"read-only recovery must reject unrelated query");
   require(f.command_queue_.empty(),"unrelated read cannot create queue work");
  } else if(scenario=="terminal_protocol") {
   terminal_samples(f.stop_recovery_);
  } else throw std::runtime_error("unknown scenario");
  std::cout << scenario << ": PASS\n";
  return 0;
 } catch(const std::exception &e) {
  std::cerr << scenario << ": FAIL: " << e.what() << "\n";
  return 1;
 }
}
''')
    binary = out / "sleep_recovery"
    subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I",
        str(root / "esphome/components/tesla_ble_vehicle"), "-I", str(out), str(source), "-o", str(binary)],
        check=True, capture_output=True, text=True)
    return binary


@pytest.mark.parametrize("scenario", ["sleep_recovery", "awake_recovery", "normal_background",
    "no_companion", "held", "pending", "unrelated_read", "write_rejected", "terminal_protocol",
    "wake_unavailable", "missing_vcsec_session", "unknown_after_reboot", "unknown_wake_unavailable"])
def test_production_recovery_query_admission_and_terminal_proof(recovery_sleep_binary, scenario):
    subprocess.run([str(recovery_sleep_binary), scenario], check=True, capture_output=True, text=True)
