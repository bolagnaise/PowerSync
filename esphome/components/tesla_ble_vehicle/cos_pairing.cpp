// SPDX-License-Identifier: AGPL-3.0-only
// Read-only Charge on Solar pairing evidence. Only an explicit request from the
// pairing request text entity reaches the car, and every request is a read:
// nothing here adds keys, sites or settings. No polling and no retries.
#include "tesla_ble_vehicle.h"
#include "hex_format.h"
#include <esphome/components/json/json_util.h>
#include <mbedtls/sha256.h>
#include <pb_decode.h>

namespace esphome { namespace tesla_ble_vehicle {

static const char *const PAIR_TAG = "tesla_ble_vehicle.cos_pairing";
static const char *const PAIR_SITES = "powersync_cos_sites";

static int key_slot(UniversalMessage_Domain domain) {
  if (domain == UniversalMessage_Domain_DOMAIN_VEHICLE_SECURITY) return 0;
  if (domain == UniversalMessage_Domain_DOMAIN_INFOTAINMENT) return 1;
  return -1;
}

void TeslaBLEVehicle::remember_vehicle_key(UniversalMessage_Domain domain, const Signatures_SessionInfo &info) {
  const int slot = key_slot(domain);
  // Only an uncompressed P-256 point is a usable gateway client key.
  if (slot < 0 || info.publicKey.size != 65 || info.publicKey.bytes[0] != 0x04) return;
  std::memcpy(vehicle_key_[slot], info.publicKey.bytes, 65);
  vehicle_key_length_[slot] = 65;
}

// First 8 bytes of SHA-256 over the key string's exact bytes, as hex.
static std::string fingerprint(const std::string &text) {
  uint8_t digest[32];
  mbedtls_sha256(reinterpret_cast<const unsigned char *>(text.data()), text.size(), digest, 0);
  return format_hex(digest, 8);
}

void TeslaBLEVehicle::publish_cos_pairing() {
  if (pair_state_ == nullptr) return;
  const auto &p = pair_;
  const bool sites = pair_request_.kind == cos_pairing::Kind::Sites;
  // Drop sites from the end until the record fits HA's 255-byte state limit;
  // "t" tells PowerSync the list is incomplete so it never treats it as whole.
  for (size_t shown = pair_sites_.count + 1; shown-- > 0;) {
    auto value = json::build_json([&](JsonObject root) {
      root["v"] = 1;
      root["i"] = recovery_vin_;
      root["n"] = p.nonce;
      root["m"] = std::string(1, p.phase);
      if (p.phase != 'i') root["r"] = sites ? "s" : "k";
      if (p.phase == 'f') root["err"] = p.error;
      if (p.phase != 'k') return;
      if (!sites) {
        const int slot = pair_request_.domain == 'v' ? 0 : 1;
        root["d"] = std::string(1, pair_request_.domain);
        root["k"] = format_hex(vehicle_key_[slot], vehicle_key_length_[slot]);
        return;
      }
      root["c"] = pair_sites_.total;
      if (shown < pair_sites_.total) root["t"] = 1;
      JsonArray list = root["s"].to<JsonArray>();
      for (size_t i = 0; i < shown; ++i) {
        const auto &site = pair_sites_.site[i];
        JsonArray entry = list.add<JsonArray>();
        entry.add(std::string(1, cos_pairing::classify_key(site.key)));
        entry.add(fingerprint(site.key));
        entry.add(site.din);
        if (site.has_location) {
          entry.add(cos_pairing::micro5(site.latitude));
          entry.add(cos_pairing::micro5(site.longitude));
        }
      }
    });
    if (value.size() <= 255) {
      pair_state_->publish_state(value);
      return;
    }
    if (p.phase != 'k' || !sites) return;  // a truncated record is never valid
  }
}

void TeslaBLEVehicle::request_cos_pairing(const std::string &text) {
  if (pair_state_ == nullptr || recovery_vin_.size() != 17) return;
  cos_pairing::Request request;
  if (!cos_pairing::parse_request(text, request)) {
    ESP_LOGW(PAIR_TAG, "Ignoring malformed Charge on Solar pairing request");
    return;
  }
  cos::Request slot;  // the shared state machine only needs the nonce
  slot.nonce = request.nonce;
  if (pair_.busy() || slot.nonce == pair_.nonce) {
    ESP_LOGW(PAIR_TAG, "Pairing request ignored: another is in progress or it repeats the last nonce");
    return;
  }
  pair_request_ = request;
  pair_sites_ = cos_pairing::Sites{};
  if (request.kind == cos_pairing::Kind::VehicleKey) {
    // Answered from the session info the bridge already holds; no BLE traffic.
    const int index = request.domain == 'v' ? 0 : 1;
    if (vehicle_key_length_[index] != 65) pair_.refuse(slot, "no_session_key");
    else if (pair_.begin(slot, millis())) pair_.phase = 'k';
    publish_cos_pairing();
    return;
  }
  const char *refusal = nullptr;
  if (recovery_proof_ && stop_recovery_.locked()) refusal = "recovery_locked";
  else if (this->node_state != espbt::ClientState::ESTABLISHED) refusal = "ble_disconnected";
  else if (cos_.busy()) refusal = "charge_on_solar_busy";
  if (refusal != nullptr) {
    if (pair_.refuse(slot, refusal)) publish_cos_pairing();
    return;
  }
  if (!pair_.begin(slot, millis())) return;
  ESP_LOGI(PAIR_TAG, "Managed charging sites read requested");
  publish_cos_pairing();
  enqueue_cos_pairing_sites();
}

void TeslaBLEVehicle::enqueue_cos_pairing_sites() {
  std::function<int()> execute = [this]() -> int {
    if (!pair_.busy()) return -1;
    uint8_t action[8];
    const size_t action_length = cos_pairing::encode_get_sites(action, sizeof(action));
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
    static_message_buffer_[0] = uint8_t(routable_length >> 8);
    static_message_buffer_[1] = uint8_t(routable_length);
    UniversalMessage_RoutableMessage sent = UniversalMessage_RoutableMessage_init_default;
    auto stream = pb_istream_from_buffer(static_message_buffer_ + 2, routable_length);
    uint8_t hash[17];
    const size_t hash_length = tesla_ble_client_->copyLastRequestHash(hash, sizeof(hash));
    if (!pb_decode(&stream, UniversalMessage_RoutableMessage_fields, &sent) || sent.uuid.size != 16
        || hash_length == 0 || !pair_.record(format_hex(sent.uuid.bytes, 16).c_str(), hash, hash_length))
      return -1;
    return writeBLE(static_message_buffer_, routable_length + 2, ESP_GATT_WRITE_TYPE_NO_RSP, ESP_GATT_AUTH_REQ_NONE);
  };
  // As with Charge on Solar reads, the name must not start with "get" so an
  // explicit read can use the normal authenticated wake path.
  placeAtFrontOfQueue(UniversalMessage_Domain_DOMAIN_INFOTAINMENT, execute, PAIR_SITES);
}

bool TeslaBLEVehicle::handle_cos_pairing_response(const CarServer_Response &response) {
  if (!pair_.busy() || command_queue_.empty() || read_queue_message_.request_uuid.size != 16) return false;
  if (command_queue_.front().execute_name != PAIR_SITES) return false;
  const auto *sent = pair_.find(format_hex(read_queue_message_.request_uuid.bytes, 16));
  if (sent == nullptr || !response.has_actionStatus) return false;
  pop_command_and_tidy_up();
  if (response.actionStatus.result != CarServer_OperationStatus_E_OPERATIONSTATUS_OK) {
    pair_.fail("read_rejected");
    ESP_LOGW(PAIR_TAG, "Managed charging sites read rejected");
    publish_cos_pairing();
    return true;
  }
  static uint8_t plain[UniversalMessage_RoutableMessage_size];
  size_t plain_length = 0;
  const int status = tesla_ble_client_->decryptInfotainmentResponse(
      &read_queue_message_.payload.protobuf_message_as_bytes, &read_queue_message_.sub_sigData.signature_data,
      sent->hash, sent->hash_len, plain, sizeof(plain), &plain_length);
  cos_pairing::Sites sites;
  const auto decoded = status == 0 ? cos_pairing::decode_sites_response(plain, plain_length, sites)
                                   : cos::Decode::Malformed;
  if (status != 0) pair_.fail("decrypt_failed");
  else if (decoded == cos::Decode::Absent) pair_.fail("unsupported_by_vehicle");
  else if (decoded == cos::Decode::Malformed) pair_.fail("malformed_response");
  else { pair_sites_ = sites; pair_.phase = 'k'; }
  ESP_LOGI(PAIR_TAG, "Managed charging sites read phase %c (%u sites)", pair_.phase, unsigned(pair_sites_.total));
  publish_cos_pairing();
  return true;
}

void TeslaBLEVehicle::tick_cos_pairing() {
  if (!pair_.expired(millis())) return;
  pair_.fail("timeout");
  ESP_LOGW(PAIR_TAG, "Managed charging sites read timed out");
  publish_cos_pairing();
}

}}  // namespace esphome::tesla_ble_vehicle
