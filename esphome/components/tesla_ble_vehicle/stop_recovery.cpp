// SPDX-License-Identifier: AGPL-3.0-only
#include "tesla_ble_vehicle.h"
#include <esphome/core/application.h>
#include <esphome/components/json/json_util.h>
#include <esp_random.h>
#include <nvs.h>

namespace esphome { namespace tesla_ble_vehicle {

void TeslaBLEVehicle::setup_stop_recovery() {
  char boot[17];
  snprintf(boot, sizeof(boot), "%08x%08x", unsigned(esp_random()), unsigned(esp_random()));
  StopRecoveryStore stored;
  size_t size = sizeof(stored);
  auto error = nvs_get_blob(storage_handle_, "ps_recovery_v2", &stored, &size);
  if (error != ESP_ERR_NVS_NOT_FOUND && (error != ESP_OK || size != sizeof(stored)))
    stored.magic = 0; // a damaged record never becomes unlocked defaults
  stop_recovery_.load(stored, boot);
  if (stop_recovery_.locked() && recovery_vin_ != stop_recovery_.saved.vin)
    stop_recovery_.held();
  if (stop_recovery_.locked()) {
    // This is boot-time work only, before a recovery GATT session exists.
    // Restored controls must not carry queued mutations into that session.
    command_queue_ = {};
    ble_write_queue_ = {};
    ble_read_queue_ = {};
    response_queue_ = {};
  }
  publish_stop_recovery();
}

bool TeslaBLEVehicle::persist_stop_recovery() {
  return nvs_set_blob(storage_handle_, "ps_recovery_v2", &stop_recovery_.saved,
                      sizeof(stop_recovery_.saved)) == ESP_OK && nvs_commit(storage_handle_) == ESP_OK;
}

void TeslaBLEVehicle::publish_stop_recovery() {
  if (recovery_proof_ == nullptr) return;
  const auto &p = stop_recovery_;
  auto value = json::build_json([&](JsonObject root) {
    root["v"] = 2;
    root["b"] = p.boot;
    root["o"] = p.saved.previous_boot;
    root["n"] = p.saved.nonce;
    root["g"] = p.saved.generation;
    root["m"] = std::string(1, p.phase);
    root["i"] = recovery_vin_;
    root["s"] = p.sequence;
    root["t"] = p.sample_ms;
    if (p.zero_samples == 2 && (p.phase == 'v' || p.phase == 'r')) {
      root["a"] = 0;
      root["w"] = 0;
      root["c"] = std::string(1, p.stopped_state);
    }
  });
  // HA states are limited to 255 bytes. Never truncate a proof into a valid
  // looking partial record, and never publish a timer-generated fresh sample.
  if (value.size() <= 255) recovery_proof_->publish_state(value);
}

void TeslaBLEVehicle::request_stop_recovery(const std::string &request) {
  if (recovery_proof_ == nullptr || recovery_vin_.size() != 17) return;
  auto colon = request.find(':');
  if (colon == std::string::npos) return;
  const std::string action = request.substr(0, colon), nonce = request.substr(colon + 1);
  if (!recovery_hex(nonce, 32)) return;
  if (action == "prepare") {
    if (!stop_recovery_.prepare(nonce)) return;
    recovery_vin_.copy(stop_recovery_.saved.vin, 17);
    stop_recovery_.saved.vin[17] = 0;
    if (!persist_stop_recovery()) {
      stop_recovery_.held(); publish_stop_recovery(); return;
    }
    publish_stop_recovery();
    // Only a new process/boot may claim retirement. Dropping software queues
    // in place or receiving radio-off is deliberately not a proof mechanism.
    this->set_timeout("ps_stop_reboot", 100, []() { App.safe_reboot(); });
  } else if (nonce == stop_recovery_.saved.nonce && action == "commit"
             && stop_recovery_.can_release(nonce, millis())) {
    stop_recovery_.saved.locked = false;
    if (!persist_stop_recovery()) {
      stop_recovery_.held(); publish_stop_recovery(); return;
    }
    stop_recovery_.released();
    publish_stop_recovery();
  } else if (nonce == stop_recovery_.saved.nonce && action == "hold") {
    stop_recovery_.held();
    persist_stop_recovery();
    this->parent()->set_enabled(false);
    publish_stop_recovery();
  }
}

void TeslaBLEVehicle::read_stop_recovery_sample(const CarServer_Response &response) {
  if (!stop_recovery_.reading() || read_queue_message_.request_uuid.size != 16) return;
  bool complete = response.which_response_msg == CarServer_Response_vehicleData_tag
      && response.response_msg.vehicleData.has_charge_state;
  char stopped = '?';
  float current = NAN, power = NAN;
  if (complete) {
    const auto &charge = response.response_msg.vehicleData.charge_state;
    complete = charge.has_charging_state && charge.which_optional_charger_actual_current
        && charge.which_optional_charger_power;
    if (complete) {
      current = charge.optional_charger_actual_current.charger_actual_current;
      power = charge.optional_charger_power.charger_power;
      switch (charge.charging_state.which_type) {
        case CarServer_ChargeState_ChargingState_Stopped_tag: stopped = 'S'; break;
        case CarServer_ChargeState_ChargingState_Complete_tag: stopped = 'C'; break;
        case CarServer_ChargeState_ChargingState_Disconnected_tag: stopped = 'D'; break;
        case CarServer_ChargeState_ChargingState_NoPower_tag: stopped = 'N'; break;
        default: break;
      }
    }
  }
  if (stop_recovery_.sample(format_hex(read_queue_message_.request_uuid.bytes, 16),
                             millis(), complete, stopped, current, power))
    publish_stop_recovery();
}

}}  // namespace esphome::tesla_ble_vehicle
