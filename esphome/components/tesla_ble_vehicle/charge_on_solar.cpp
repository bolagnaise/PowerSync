// SPDX-License-Identifier: AGPL-3.0-only
// Explicit Tesla native Charge on Solar read/set over the existing BLE session.
// Only an explicit request from the request text entity reaches the car.
// There is no polling, retry after a final result, or automatic re-enable.
#include "tesla_ble_vehicle.h"
#include "hex_format.h"
#include <esphome/components/json/json_util.h>
#include <pb_decode.h>

namespace esphome { namespace tesla_ble_vehicle {

static const char *const COS_TAG = "tesla_ble_vehicle.cos";
static const char *const COS_READ = "powersync_cos_read";
static const char *const COS_WRITE = "powersync_cos_write";

void TeslaBLEVehicle::publish_charge_on_solar() {
  if (cos_state_ == nullptr) return;
  const auto &p = cos_;
  auto value = json::build_json([&](JsonObject root) {
    root["v"] = 1;
    root["i"] = recovery_vin_;
    root["n"] = p.nonce;
    root["m"] = std::string(1, p.phase);
    if (p.phase != 'i') root["w"] = p.request.write ? 1 : 0;
    if (p.request.write && p.write_acked) root["a"] = 1;
    if (p.phase == 'k') {
      root["e"] = p.observed.enabled ? 1 : 0;
      root["lo"] = p.observed.lower;
      root["up"] = p.observed.upper;
    }
    if (p.phase == 'f' || p.phase == 'u') root["err"] = p.error;
  });
  // HA states are limited to 255 bytes. A truncated record is never valid.
  if (value.size() <= 255) cos_state_->publish_state(value);
}

void TeslaBLEVehicle::request_charge_on_solar(const std::string &text) {
  if (cos_state_ == nullptr || recovery_vin_.size() != 17) return;
  cos::Request request;
  if (!cos::parse_request(text, request)) {
    ESP_LOGW(COS_TAG, "Ignoring malformed Charge on Solar request");
    return;
  }
  const char *refusal = nullptr;
  if (recovery_proof_ && stop_recovery_.locked()) refusal = "recovery_locked";
  else if (this->node_state != espbt::ClientState::ESTABLISHED) refusal = "ble_disconnected";
  if (refusal != nullptr) {
    if (cos_.refuse(request, refusal)) publish_charge_on_solar();
    return;
  }
  if (!cos_.begin(request, millis())) {
    ESP_LOGW(COS_TAG, "Charge on Solar request ignored: another request is in progress or it repeats the last nonce");
    return;
  }
  ESP_LOGI(COS_TAG, "Charge on Solar %s requested", request.write ? "write" : "read");
  publish_charge_on_solar();
  enqueue_charge_on_solar(request.write);
}

void TeslaBLEVehicle::enqueue_charge_on_solar(bool write) {
  std::function<int()> execute = [this, write]() -> int {
    if (!cos_.busy() || cos_.reading_back == write) return -1;
    uint8_t action[40];
    size_t action_length = write ? cos::encode_set(cos_.request.settings, action, sizeof(action))
                                 : cos::encode_get(action, sizeof(action));
    if (action_length == 0) return -1;
    size_t routable_length = UniversalMessage_RoutableMessage_size;
    int status = tesla_ble_client_->buildUniversalMessageWithPayload(
        action, action_length, UniversalMessage_Domain_DOMAIN_INFOTAINMENT,
        static_message_buffer_ + 2, &routable_length, true);
    if (status != TeslaBLE::TeslaBLE_Status_E_OK) {
      if (status == TeslaBLE::TeslaBLE_Status_E_ERROR_INVALID_SESSION)
        invalidateSession(UniversalMessage_Domain_DOMAIN_INFOTAINMENT);
      return status;
    }
    if (routable_length > 0xffff || routable_length + 2 > sizeof(static_message_buffer_)) return -1;
    // Same two-byte big-endian BLE frame length the library prepends.
    static_message_buffer_[0] = uint8_t(routable_length >> 8);
    static_message_buffer_[1] = uint8_t(routable_length);
    const size_t frame_length = routable_length + 2;
    // Correlate the reply by the UUID inside the exact frame being sent.
    UniversalMessage_RoutableMessage sent = UniversalMessage_RoutableMessage_init_default;
    auto stream = pb_istream_from_buffer(static_message_buffer_ + 2, routable_length);
    uint8_t hash[17];
    size_t hash_length = tesla_ble_client_->copyLastRequestHash(hash, sizeof(hash));
    if (!pb_decode(&stream, UniversalMessage_RoutableMessage_fields, &sent) || sent.uuid.size != 16
        || hash_length == 0 || !cos_.record(format_hex(sent.uuid.bytes, 16).c_str(), hash, hash_length))
      return -1;
    return writeBLE(static_message_buffer_, frame_length, ESP_GATT_WRITE_TYPE_NO_RSP, ESP_GATT_AUTH_REQ_NONE);
  };
  // Names deliberately do not start with "get": an explicit read may use the
  // normal authenticated wake path instead of being dropped while asleep.
  placeAtFrontOfQueue(UniversalMessage_Domain_DOMAIN_INFOTAINMENT, execute, write ? COS_WRITE : COS_READ);
}

bool TeslaBLEVehicle::handle_charge_on_solar_response(const CarServer_Response &response) {
  if (!cos_.busy() || command_queue_.empty() || read_queue_message_.request_uuid.size != 16) return false;
  const std::string &name = command_queue_.front().execute_name;
  const bool write = name == COS_WRITE;
  if (!write && name != COS_READ) return false;
  const auto *sent = cos_.find(format_hex(read_queue_message_.request_uuid.bytes, 16));
  if (sent == nullptr || !response.has_actionStatus) return false;
  pop_command_and_tidy_up();
  if (response.actionStatus.result != CarServer_OperationStatus_E_OPERATIONSTATUS_OK) {
    std::string reason = write ? "write_rejected" : "read_rejected";
    if (response.actionStatus.has_result_reason
        && response.actionStatus.result_reason.which_reason == CarServer_ResultReason_plain_text_tag) {
      std::string text = response.actionStatus.result_reason.reason.plain_text;
      // Keep to the character set HA accepts, so a real rejection is never
      // mistaken for an invalid record and reported as a timeout.
      for (char &c : text)
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')
              || c == '_' || c == '.' || c == '-' || c == ' ')) c = '_';
      reason += ":" + text.substr(0, 48);
    }
    cos_.fail(reason, write);
    ESP_LOGW(COS_TAG, "Charge on Solar %s", reason.c_str());
    publish_charge_on_solar();
    return true;
  }
  if (write) {
    cos_.acknowledged();
    ESP_LOGI(COS_TAG, "Charge on Solar write acknowledged; reading back");
    publish_charge_on_solar();
    // Give the car a moment to apply the setting before the readback.
    this->set_timeout("ps_cos_readback", 1000, [this]() {
      if (cos_.busy() && cos_.reading_back) enqueue_charge_on_solar(false);
    });
    return true;
  }
  static uint8_t plain[UniversalMessage_RoutableMessage_size];
  size_t plain_length = 0;
  int status = tesla_ble_client_->decryptInfotainmentResponse(
      &read_queue_message_.payload.protobuf_message_as_bytes, &read_queue_message_.sub_sigData.signature_data,
      sent->hash, sent->hash_len, plain, sizeof(plain), &plain_length);
  cos::Settings observed;
  cos::Decode decoded = status == 0 ? cos::decode_response(plain, plain_length, observed) : cos::Decode::Malformed;
  if (status != 0) cos_.fail("decrypt_failed");
  else if (decoded == cos::Decode::Absent) cos_.fail("unsupported_by_vehicle");
  else if (decoded == cos::Decode::Malformed) cos_.fail("malformed_response");
  else cos_.confirmed(observed);
  ESP_LOGI(COS_TAG, "Charge on Solar readback phase %c", cos_.phase);
  publish_charge_on_solar();
  return true;
}

void TeslaBLEVehicle::tick_charge_on_solar() {
  if (!cos_.expired(millis())) return;
  cos_.fail("timeout");
  ESP_LOGW(COS_TAG, "Charge on Solar request timed out (phase %c)", cos_.phase);
  publish_charge_on_solar();
}

}}  // namespace esphome::tesla_ble_vehicle
