// SPDX-License-Identifier: AGPL-3.0-only
// Charge on Solar pairing over BLE. Only an explicit request from the pairing
// request text entity reaches the car. Reads report evidence; each write is
// sent once and never retried. There is no polling.
//
// Writes:
//   wla  add the gateway's key as ROLE_CHARGING_MANAGER. Unsigned; the car
//        only accepts it after a key-card tap, so the bridge reports "p"
//        (sent, awaiting tap) and PowerSync confirms with an "ent" read.
//   ste  add a managed charging site, then read the sites back.
//   rms  remove a managed charging site, then read the sites back.
#include "tesla_ble_vehicle.h"
#include "hex_format.h"
#include <esphome/components/json/json_util.h>
#include <mbedtls/sha1.h>
#include <mbedtls/sha256.h>
#include <pb_decode.h>

namespace esphome { namespace tesla_ble_vehicle {

static const char *const PAIR_TAG = "tesla_ble_vehicle.cos_pairing";
static const char *const PAIR_SITES = "powersync_cos_sites";
static const char *const PAIR_SITE_WRITE = "powersync_cos_site_write";
// PowerSync waits 90 s for the tap; hold VCSEC commands slightly longer.
static constexpr uint32_t KEY_ADD_HOLD_MS = 95000;

using cos_pairing::Kind;

static int key_slot(UniversalMessage_Domain domain) {
  if (domain == UniversalMessage_Domain_DOMAIN_VEHICLE_SECURITY) return 0;
  if (domain == UniversalMessage_Domain_DOMAIN_INFOTAINMENT) return 1;
  return -1;
}

static char record_kind(Kind kind) {
  switch (kind) {
    case Kind::Sites: return 's';
    case Kind::VehicleKey: return 'k';
    case Kind::Entry: return 'e';
    case Kind::AddKey: return 'a';
    case Kind::AddSite: return 't';
    case Kind::RemoveSite: return 'q';
  }
  return 's';
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
  const Kind kind = pair_request_.kind;
  const bool lists_sites = kind == Kind::Sites || kind == Kind::AddSite || kind == Kind::RemoveSite;
  // Drop sites from the end until the record fits HA's 255-byte state limit;
  // "t" tells PowerSync the list is incomplete so it never treats it as whole.
  for (size_t shown = pair_sites_.count + 1; shown-- > 0;) {
    auto value = json::build_json([&](JsonObject root) {
      root["v"] = 1;
      root["i"] = recovery_vin_;
      root["n"] = p.nonce;
      root["m"] = std::string(1, p.phase);
      if (p.phase == 'i') return;
      root["r"] = std::string(1, record_kind(kind));
      if (p.phase == 'f' || p.phase == 'u') root["err"] = p.error;
      if (p.phase == 'u' && p.write_acked) root["a"] = 1;
      if (p.phase != 'k') return;
      if (kind == Kind::VehicleKey) {
        const int slot = pair_request_.domain == 'v' ? 0 : 1;
        root["d"] = std::string(1, pair_request_.domain);
        root["k"] = format_hex(vehicle_key_[slot], vehicle_key_length_[slot]);
        return;
      }
      if (kind == Kind::Entry) {
        root["p"] = pair_entry_role_ >= 0 ? 1 : 0;
        if (pair_entry_role_ >= 0) root["o"] = pair_entry_role_;
        return;
      }
      if (!lists_sites) return;
      if (kind == Kind::AddSite) root["x"] = pair_site_outcome_;
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
    if (p.phase != 'k' || !lists_sites) return;  // a truncated record is never valid
  }
}

void TeslaBLEVehicle::request_cos_pairing(const std::string &text) {
  if (pair_state_ == nullptr || recovery_vin_.size() != 17) return;
  cos_pairing::Request request;
  if (!cos_pairing::parse_request(text, request)) {
    ESP_LOGW(PAIR_TAG, "Ignoring malformed Charge on Solar pairing request");
    return;
  }
  cos::Request slot;  // the shared state machine needs the nonce and direction
  slot.nonce = request.nonce;
  slot.write = request.write();
  if (pair_.busy() || slot.nonce == pair_.nonce) {
    ESP_LOGW(PAIR_TAG, "Pairing request ignored: another is in progress or it repeats the last nonce");
    return;
  }
  pair_request_ = request;
  pair_sites_ = cos_pairing::Sites{};
  pair_entry_role_ = -1;
  pair_site_outcome_ = 0;
  if (request.kind == Kind::VehicleKey) {
    // Answered from the session info the bridge already holds; no BLE traffic.
    const int index = request.domain == 'v' ? 0 : 1;
    if (vehicle_key_length_[index] != 65) pair_.refuse(slot, "no_session_key");
    else if (pair_.begin(slot, millis())) pair_.phase = 'k';
    publish_cos_pairing();
    return;
  }
  const bool vcsec = request.kind == Kind::Entry || request.kind == Kind::AddKey;
  const char *refusal = nullptr;
  if (recovery_proof_ && stop_recovery_.locked()) refusal = "recovery_locked";
  else if (this->node_state != espbt::ClientState::ESTABLISHED) refusal = "ble_disconnected";
  else if (cos_.busy()) refusal = "charge_on_solar_busy";
  // A VCSEC reply carries no request ID the generic handler checks, so never
  // send one while a started VCSEC command could mistake it for its own answer.
  else if (vcsec && vcsec_command_in_flight()) refusal = "command_queue_busy";
  else if (request.kind == Kind::AddKey && key_add_pending(millis())) refusal = "key_add_pending";
  if (refusal != nullptr) {
    if (pair_.refuse(slot, refusal)) publish_cos_pairing();
    return;
  }
  if (!pair_.begin(slot, millis())) return;
  publish_cos_pairing();
  if (!vcsec) {
    ESP_LOGI(PAIR_TAG, "Managed charging site %s requested", request.write() ? "write" : "read");
    enqueue_cos_pairing_carserver(request.write());
    return;
  }
  size_t length = 0;
  int status = request.kind == Kind::Entry
      ? tesla_ble_client_->buildWhitelistEntryRequest(request.key, 65, static_message_buffer_, &length)
      : tesla_ble_client_->buildWhitelistAddForKey(request.key, 65, Keys_Role_ROLE_CHARGING_MANAGER,
                                                    static_message_buffer_, &length);
  if (status != 0 || length == 0 || length > sizeof(static_message_buffer_)) {
    pair_.fail("build_failed");  // nothing left the bridge
    publish_cos_pairing();
    return;
  }
  // Sent exactly once. The car answers an add only after a key-card tap, and
  // a resend would be a second, separate add request.
  status = writeBLE(static_message_buffer_, length, ESP_GATT_WRITE_TYPE_NO_RSP, ESP_GATT_AUTH_REQ_NONE);
  if (status != 0) {
    pair_.fail("ble_write_failed");
    publish_cos_pairing();
    return;
  }
  if (request.kind == Kind::AddKey) {
    ESP_LOGI(PAIR_TAG, "Charging-manager key add sent; tap a key card on the car to approve it");
    // Hold queued VCSEC commands until the add resolves or the tap window ends.
    pair_key_add_until_ = millis() + KEY_ADD_HOLD_MS;
    if (pair_key_add_until_ == 0) pair_key_add_until_ = 1;
    pair_.phase = 'p';
    publish_cos_pairing();
  } else {
    ESP_LOGI(PAIR_TAG, "Whitelist entry read sent");
  }
}

void TeslaBLEVehicle::enqueue_cos_pairing_carserver(bool write) {
  std::function<int()> execute = [this, write]() -> int {
    if (!pair_.busy() || pair_.reading_back == write) return -1;
    static uint8_t action[cos_pairing::MAX_ACTION];
    size_t action_length = 0;
    if (!write) action_length = cos_pairing::encode_get_sites(action, sizeof(action));
    else if (pair_request_.kind == Kind::AddSite) action_length = cos_pairing::encode_add_site(pair_request_.site, action, sizeof(action));
    else action_length = cos_pairing::encode_remove_site(pair_request_.site.key, action, sizeof(action));
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
  // Names must not start with "get" so an explicit request can use the
  // normal authenticated wake path instead of being dropped while asleep.
  placeAtFrontOfQueue(UniversalMessage_Domain_DOMAIN_INFOTAINMENT, execute, write ? PAIR_SITE_WRITE : PAIR_SITES);
}

bool TeslaBLEVehicle::handle_cos_pairing_response(const CarServer_Response &response) {
  if (!pair_.busy() || command_queue_.empty() || read_queue_message_.request_uuid.size != 16) return false;
  const std::string &name = command_queue_.front().execute_name;
  const bool write = name == PAIR_SITE_WRITE;
  if (!write && name != PAIR_SITES) return false;
  const auto *sent = pair_.find(format_hex(read_queue_message_.request_uuid.bytes, 16));
  if (sent == nullptr || !response.has_actionStatus) return false;
  pop_command_and_tidy_up();
  if (response.actionStatus.result != CarServer_OperationStatus_E_OPERATIONSTATUS_OK) {
    pair_.fail(write ? "write_rejected" : "read_rejected", write);
    ESP_LOGW(PAIR_TAG, "Managed charging site %s rejected", write ? "write" : "read");
    publish_cos_pairing();
    return true;
  }
  static uint8_t plain[UniversalMessage_RoutableMessage_size];
  size_t plain_length = 0;
  const int status = tesla_ble_client_->decryptInfotainmentResponse(
      &read_queue_message_.payload.protobuf_message_as_bytes, &read_queue_message_.sub_sigData.signature_data,
      sent->hash, sent->hash_len, plain, sizeof(plain), &plain_length);
  if (write) {
    // The car accepted the write. Its outcome detail is best-effort; the
    // readback is what confirms the result.
    uint8_t outcome = 0;
    if (status == 0 && pair_request_.kind == Kind::AddSite
        && cos_pairing::decode_add_site_response(plain, plain_length, outcome) == cos::Decode::Ok)
      pair_site_outcome_ = outcome;
    pair_.acknowledged();
    ESP_LOGI(PAIR_TAG, "Managed charging site write acknowledged; reading back");
    publish_cos_pairing();
    this->set_timeout("ps_cos_site_readback", 1000, [this]() {
      if (pair_.busy() && pair_.reading_back) enqueue_cos_pairing_carserver(false);
    });
    return true;
  }
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

// The key ID is the first 4 bytes of SHA-1 over the raw point.
static bool same_key_id(const uint8_t (&key)[65], const VCSEC_KeyIdentifier &id) {
  uint8_t digest[20];
  mbedtls_sha1(key, 65, digest);
  return id.publicKeySHA1.size == 4 && std::memcmp(id.publicKeySHA1.bytes, digest, 4) == 0;
}

bool TeslaBLEVehicle::handle_cos_pairing_entry(const VCSEC_WhitelistEntryInfo &info) {
  if (!pair_.busy() || pair_request_.kind != Kind::Entry) return false;
  const bool by_key = info.has_publicKey && info.publicKey.PublicKeyRaw.size == 65
      && std::memcmp(info.publicKey.PublicKeyRaw.bytes, pair_request_.key, 65) == 0;
  const bool by_id = !info.has_publicKey && info.has_keyId && same_key_id(pair_request_.key, info.keyId);
  if (!by_key && !by_id) return false;  // an entry for some other key
  pair_entry_role_ = int(info.keyRole);
  // The key is on the car, so the add has resolved.
  pair_key_add_until_ = 0;
  pair_.phase = 'k';
  ESP_LOGI(PAIR_TAG, "Whitelist entry found with role %d", pair_entry_role_);
  publish_cos_pairing();
  return true;
}

bool TeslaBLEVehicle::handle_cos_pairing_entry_error(int generic_error) {
  if (!pair_.busy() || pair_request_.kind != Kind::Entry) return false;
  // The car names no key in this answer, so it only proves the entry was not
  // returned, not that it is absent.
  pair_.fail("entry_not_returned:" + std::to_string(generic_error));
  publish_cos_pairing();
  return true;
}

void TeslaBLEVehicle::tick_cos_pairing() {
  if (!pair_.expired(millis())) return;
  pair_.fail("timeout");
  ESP_LOGW(PAIR_TAG, "Pairing request timed out (phase %c)", pair_.phase);
  publish_cos_pairing();
}

}}  // namespace esphome::tesla_ble_vehicle
