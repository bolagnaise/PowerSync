// SPDX-License-Identifier: AGPL-3.0-only
// Narrow extension of the pinned TeslaBLE client for CarServer messages the
// library's nanopb schema does not know. It adds no transport or signing of
// its own: framing, session, counters, encryption and response
// authentication all stay in the library.
#pragma once
#include <cstdint>
#include <cstring>
#include <client.h>
#include <peer.h>
#include <errors.h>

namespace esphome { namespace tesla_ble_vehicle {

class PowerSyncBLEClient : public TeslaBLE::Client {
 public:
  // Hash of the most recently built request, needed to authenticate and
  // decrypt that request's AES-GCM response. Copy it right after building.
  size_t copyLastRequestHash(uint8_t *out, size_t cap) const {
    if (last_request_hash_length_ == 0 || last_request_hash_length_ > cap) return 0;
    std::memcpy(out, last_request_hash_, last_request_hash_length_);
    return last_request_hash_length_;
  }

  // Decrypt an infotainment response to its raw CarServer.Response bytes.
  // Peer::DecryptResponse is const and keeps no replay state, so this can run
  // alongside the library's own nanopb parse of the same message.
  int decryptInfotainmentResponse(
      const UniversalMessage_RoutableMessage_protobuf_message_as_bytes_t *input,
      Signatures_SignatureData *signature_data, const uint8_t *request_hash,
      size_t request_hash_length, uint8_t *output, size_t output_cap, size_t *output_length) {
    if (signature_data->which_sig_type != Signatures_SignatureData_AES_GCM_Response_data_tag)
      return TeslaBLE::TeslaBLE_Status_E_ERROR_DECRYPT;
    auto session = getPeer(UniversalMessage_Domain_DOMAIN_INFOTAINMENT);
    if (session == nullptr || !session->isInitialized())
      return TeslaBLE::TeslaBLE_Status_E_ERROR_INVALID_SESSION;
    return session->DecryptResponse(
        input->bytes, input->size,
        signature_data->sig_type.AES_GCM_Response_data.nonce,
        signature_data->sig_type.AES_GCM_Response_data.tag,
        request_hash, request_hash_length,
        UniversalMessage_Flags_FLAG_ENCRYPT_RESPONSE,
        UniversalMessage_MessageFault_E_MESSAGEFAULT_ERROR_NONE,
        output, output_cap, output_length);
  }
};

}}  // namespace esphome::tesla_ble_vehicle
