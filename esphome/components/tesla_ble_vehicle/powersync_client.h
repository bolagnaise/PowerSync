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
#include <tb_utils.h>

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

  // Unsigned "add key" request for another device's key, approved on the car
  // by a key-card tap. It mirrors the library's buildWhiteListMessage, which
  // can only add this bridge's own key. No key metadata is sent, matching the
  // Charge on Solar setup message, and the bridge's own key is untouched.
  int buildWhitelistAddForKey(const uint8_t *key, size_t key_length, Keys_Role role,
                              pb_byte_t *output, size_t *output_length) {
    if (key_length != 65 || key[0] != 0x04) return TeslaBLE::TeslaBLE_Status_E_ERROR_INTERNAL;
    unsigned_message_ = VCSEC_UnsignedMessage_init_default;
    unsigned_message_.which_sub_message = VCSEC_UnsignedMessage_WhitelistOperation_tag;
    auto &operation = unsigned_message_.sub_message.WhitelistOperation;
    operation = VCSEC_WhitelistOperation_init_default;
    operation.which_sub_message = VCSEC_WhitelistOperation_addKeyToWhitelistAndAddPermissions_tag;
    auto &change = operation.sub_message.addKeyToWhitelistAndAddPermissions;
    change = VCSEC_PermissionChange_init_default;
    change.has_key = true;
    std::memcpy(change.key.PublicKeyRaw.bytes, key, key_length);
    change.key.PublicKeyRaw.size = key_length;
    change.keyRole = role;
    static pb_byte_t payload[VCSEC_UnsignedMessage_size];
    size_t payload_length = 0;
    if (TeslaBLE::pb_encode_fields(payload, &payload_length, VCSEC_UnsignedMessage_fields, &unsigned_message_) != 0)
      return TeslaBLE::TeslaBLE_Status_E_ERROR_PB_ENCODING;
    static VCSEC_ToVCSECMessage message;
    message = VCSEC_ToVCSECMessage_init_default;
    message.has_signedMessage = true;
    message.signedMessage = VCSEC_SignedMessage_init_default;
    message.signedMessage.signatureType = VCSEC_SignatureType_SIGNATURE_TYPE_PRESENT_KEY;
    std::memcpy(message.signedMessage.protobufMessageAsBytes.bytes, payload, payload_length);
    message.signedMessage.protobufMessageAsBytes.size = payload_length;
    size_t encoded = 0;
    if (TeslaBLE::pb_encode_fields(output + 2, &encoded, VCSEC_ToVCSECMessage_fields, &message) != 0)
      return TeslaBLE::TeslaBLE_Status_E_ERROR_PB_ENCODING;
    insertLength(encoded, output, output_length);
    return TeslaBLE::TeslaBLE_Status_E_OK;
  }

  // Unsigned VCSEC read of the whitelist entry for one public key.
  int buildWhitelistEntryRequest(const uint8_t *key, size_t key_length,
                                 pb_byte_t *output, size_t *output_length) {
    if (key_length != 65 || key[0] != 0x04) return TeslaBLE::TeslaBLE_Status_E_ERROR_INTERNAL;
    unsigned_message_ = VCSEC_UnsignedMessage_init_default;
    unsigned_message_.which_sub_message = VCSEC_UnsignedMessage_InformationRequest_tag;
    auto &request = unsigned_message_.sub_message.InformationRequest;
    request = VCSEC_InformationRequest_init_zero;
    request.informationRequestType = VCSEC_InformationRequestType_INFORMATION_REQUEST_TYPE_GET_WHITELIST_ENTRY_INFO;
    request.which_key = VCSEC_InformationRequest_publicKey_tag;
    std::memcpy(request.key.publicKey.bytes, key, key_length);
    request.key.publicKey.size = key_length;
    return buildUnsignedMessagePayload(&unsigned_message_, output, output_length, false);
  }
};

}}  // namespace esphome::tesla_ble_vehicle
