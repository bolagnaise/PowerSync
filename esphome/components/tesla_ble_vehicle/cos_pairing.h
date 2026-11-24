// SPDX-License-Identifier: AGPL-3.0-only
// Charge on Solar pairing over BLE: read-only evidence plus explicit,
// single-shot pairing writes. Pure protocol helpers with no ESPHome, BLE,
// mbedtls or nanopb dependency so they can be host-tested.
//
// Wire layout (tesla_protocol 6.3.0 car_server.proto, absent from the pinned
// TeslaBLE nanopb headers, so decoded here by field number):
//   VehicleAction.getManagedChargingSitesRequest = 73   (empty message)
//   Response.getManagedChargingSitesResponse     = 14
//   GetManagedChargingSitesResponse{repeated ManagedChargingSite sites = 1}
//   ManagedChargingSite{string public_key = 1; ManagerType manager_type = 2;
//                       LatLong lat_lon = 3}
//   ManagerType{SiteController site_controller = 1}; SiteController{string din = 1}
//   LatLong{float latitude = 1; float longitude = 2}
//
// public_key is a protobuf string whose text encoding of the gateway's EC
// point is not documented. The bridge reports how each string is shaped and
// a SHA-256 fingerprint of the string's exact bytes; PowerSync compares that
// with the candidate encodings of the gateway key it reads over the LAN.
#pragma once
#include <cmath>
#include <cstdint>
#include <cstddef>
#include <string>
#include "charge_on_solar.h"

namespace esphome { namespace tesla_ble_vehicle { namespace cos_pairing {

static constexpr size_t MAX_SITES = 4;
static constexpr size_t MAX_KEY_TEXT = 192;
static constexpr size_t MAX_DIN = 40;

inline size_t encode_get_sites(uint8_t *out, size_t cap) {
  return cos::wrap_vehicle_action(73, nullptr, 0, out, cap);
}

struct Site {
  std::string key;  // exact public_key string bytes
  std::string din;
  bool has_location{false};
  float latitude{0}, longitude{0};
};

struct Sites {
  Site site[MAX_SITES];
  size_t count{0};      // sites decoded into site[]
  size_t total{0};      // sites the car reported
};

// Shape of a public_key string, so a fingerprint mismatch can still say
// which encoding the car used:
//   h  130 lowercase hex chars starting "04" (raw uncompressed point)
//   H  the same in uppercase hex
//   r  88 base64 chars (65-byte raw point)
//   d  124 base64 chars (91-byte SubjectPublicKeyInfo DER)
//   e  empty
//   o  anything else
inline char classify_key(const std::string &k) {
  if (k.empty()) return 'e';
  if (k.size() == 130 && k[0] == '0' && k[1] == '4') {
    bool lower = true, upper = true;
    for (char c : k) {
      const bool digit = c >= '0' && c <= '9';
      lower = lower && (digit || (c >= 'a' && c <= 'f'));
      upper = upper && (digit || (c >= 'A' && c <= 'F'));
    }
    if (lower) return 'h';
    if (upper) return 'H';
  }
  auto base64 = [&](size_t length) {
    if (k.size() != length) return false;
    for (size_t i = 0; i < k.size(); ++i) {
      const char c = k[i];
      const bool alnum = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9');
      const bool pad = c == '=' && i >= k.size() - 2;
      if (!(alnum || c == '+' || c == '/' || pad)) return false;
    }
    return true;
  };
  if (base64(88)) return 'r';
  if (base64(124)) return 'd';
  return 'o';
}

// DINs are printable ASCII identifiers such as "1232100-00-E--TG1...".
inline bool printable(const std::string &s) {
  for (char c : s) if (c < 0x21 || c > 0x7e || c == '"' || c == '\\') return false;
  return true;
}

using cos::Decode;
using cos::Reader;

inline bool read_float(const uint8_t *raw, float &out) {
  uint32_t bits = uint32_t(raw[0]) | uint32_t(raw[1]) << 8 | uint32_t(raw[2]) << 16 | uint32_t(raw[3]) << 24;
  std::memcpy(&out, &bits, 4);
  return std::isfinite(out);
}

inline Decode decode_site(const uint8_t *data, size_t len, Site &out) {
  Site s;
  Reader r{data, len};
  while (!r.done()) {
    uint32_t number; uint8_t wire; const uint8_t *v; size_t size;
    if (!r.field(number, wire, v, size)) return Decode::Malformed;
    if (number == 1) {
      if (wire != 2 || size > MAX_KEY_TEXT) return Decode::Malformed;
      s.key.assign(reinterpret_cast<const char *>(v), size);
    } else if (number == 2) {
      if (wire != 2) return Decode::Malformed;
      Reader m{v, size};
      while (!m.done()) {
        uint32_t n2; uint8_t w2; const uint8_t *v2; size_t s2;
        if (!m.field(n2, w2, v2, s2)) return Decode::Malformed;
        if (n2 != 1) continue;  // other manager types are reported without a DIN
        if (w2 != 2) return Decode::Malformed;
        Reader c{v2, s2};
        while (!c.done()) {
          uint32_t n3; uint8_t w3; const uint8_t *v3; size_t s3;
          if (!c.field(n3, w3, v3, s3)) return Decode::Malformed;
          if (n3 != 1) continue;
          if (w3 != 2 || s3 > MAX_DIN) return Decode::Malformed;
          s.din.assign(reinterpret_cast<const char *>(v3), s3);
          if (!printable(s.din)) return Decode::Malformed;
        }
      }
    } else if (number == 3) {
      if (wire != 2) return Decode::Malformed;
      s.has_location = true;
      Reader l{v, size};
      while (!l.done()) {
        uint32_t n2; uint8_t w2; const uint8_t *v2; size_t s2;
        if (!l.field(n2, w2, v2, s2)) return Decode::Malformed;
        if (n2 != 1 && n2 != 2) continue;
        if (w2 != 5 || !read_float(v2, n2 == 1 ? s.latitude : s.longitude)) return Decode::Malformed;
      }
      if (std::fabs(s.latitude) > 90 || std::fabs(s.longitude) > 180) return Decode::Malformed;
    }
  }
  out = s;
  return Decode::Ok;
}

// Find Response.getManagedChargingSitesResponse(14).sites(1). A present but
// empty response is a valid answer of zero sites.
inline Decode decode_sites_response(const uint8_t *data, size_t len, Sites &out) {
  Reader r{data, len};
  bool found = false;
  Sites result;
  while (!r.done()) {
    uint32_t number; uint8_t wire; const uint8_t *v; size_t size;
    if (!r.field(number, wire, v, size)) return Decode::Malformed;
    if (number != 14) continue;
    if (wire != 2 || found) return Decode::Malformed;
    found = true;
    Reader inner{v, size};
    while (!inner.done()) {
      uint32_t n2; uint8_t w2; const uint8_t *v2; size_t s2;
      if (!inner.field(n2, w2, v2, s2)) return Decode::Malformed;
      if (n2 != 1) continue;
      if (w2 != 2) return Decode::Malformed;
      Site site;
      if (decode_site(v2, s2, site) != Decode::Ok) return Decode::Malformed;
      if (result.count < MAX_SITES) result.site[result.count++] = site;
      ++result.total;
    }
  }
  if (!found) return Decode::Absent;
  out = result;
  return Decode::Ok;
}

// Coordinates are published as integer 1e-5 degrees (about 1 m), which keeps
// the record small and avoids float formatting differences.
inline int32_t micro5(float degrees) { return int32_t(std::lround(double(degrees) * 1e5)); }

// ---- site writes -------------------------------------------------------------
//   VehicleAction.addManagedChargingSiteRequest    = 67
//   VehicleAction.removeManagedChargingSiteRequest = 68
//   AddManagedChargingSiteRequest{ManagedChargingSite site = 1}
//   RemoveManagedChargingSiteRequest{string public_key = 1}
//   Response.addManagedChargingSiteResponse = 16
//     {ManagedChargingSiteAddedOrUpdated added_or_updated = 1;
//      ExistingManagedChargingSiteNearby existing_site_nearby = 2}

static constexpr size_t MAX_ACTION = 256;

inline size_t wrap(uint32_t field, const uint8_t *member, size_t n, uint8_t *out, size_t cap) {
  uint8_t inner[MAX_ACTION];
  cos::Writer w{inner, sizeof(inner)};
  w.bytes(field, member, n);
  if (!w.ok) return 0;
  cos::Writer a{out, cap};
  a.bytes(2, inner, w.len);
  return a.ok ? a.len : 0;
}

inline bool valid_site(const Site &s) {
  return !s.key.empty() && s.key.size() <= MAX_KEY_TEXT && classify_key(s.key) != 'o'
         && !s.din.empty() && s.din.size() <= MAX_DIN && printable(s.din) && s.has_location
         && std::isfinite(s.latitude) && std::isfinite(s.longitude)
         && std::fabs(s.latitude) <= 90 && std::fabs(s.longitude) <= 180;
}

inline size_t encode_add_site(const Site &s, uint8_t *out, size_t cap) {
  if (!valid_site(s)) return 0;
  uint8_t controller[MAX_DIN + 4], manager[MAX_DIN + 8], location[16], site[MAX_ACTION], request[MAX_ACTION];
  cos::Writer c{controller, sizeof(controller)};
  c.bytes(1, reinterpret_cast<const uint8_t *>(s.din.data()), s.din.size());
  cos::Writer m{manager, sizeof(manager)};
  m.bytes(1, controller, c.len);
  cos::Writer l{location, sizeof(location)};
  if (s.latitude != 0) { l.tag(1, 5); l.fixed32(s.latitude); }
  if (s.longitude != 0) { l.tag(2, 5); l.fixed32(s.longitude); }
  cos::Writer w{site, sizeof(site)};
  w.bytes(1, reinterpret_cast<const uint8_t *>(s.key.data()), s.key.size());
  w.bytes(2, manager, m.len);
  w.bytes(3, location, l.len);
  cos::Writer r{request, sizeof(request)};
  r.bytes(1, site, w.len);
  if (!c.ok || !m.ok || !l.ok || !w.ok || !r.ok) return 0;
  return wrap(67, request, r.len, out, cap);
}

inline size_t encode_remove_site(const std::string &key, uint8_t *out, size_t cap) {
  if (key.empty() || key.size() > MAX_KEY_TEXT || classify_key(key) == 'o') return 0;
  uint8_t request[MAX_KEY_TEXT + 4];
  cos::Writer r{request, sizeof(request)};
  r.bytes(1, reinterpret_cast<const uint8_t *>(key.data()), key.size());
  if (!r.ok) return 0;
  return wrap(68, request, r.len, out, cap);
}

// Response.addManagedChargingSiteResponse(16): 1 added or updated, 2 the car
// kept an existing site nearby instead. Absent means the car did not say.
inline Decode decode_add_site_response(const uint8_t *data, size_t len, uint8_t &outcome) {
  Reader r{data, len};
  uint8_t found = 0;
  while (!r.done()) {
    uint32_t number; uint8_t wire; const uint8_t *v; size_t size;
    if (!r.field(number, wire, v, size)) return Decode::Malformed;
    if (number != 16) continue;
    if (wire != 2 || found) return Decode::Malformed;
    Reader inner{v, size};
    found = 3;  // present but empty: acknowledged without detail
    while (!inner.done()) {
      uint32_t n2; uint8_t w2; const uint8_t *v2; size_t s2;
      if (!inner.field(n2, w2, v2, s2)) return Decode::Malformed;
      if ((n2 == 1 || n2 == 2) && w2 != 2) return Decode::Malformed;
      if (n2 == 1 || n2 == 2) found = uint8_t(n2);
    }
  }
  if (!found) return Decode::Absent;
  outcome = found;
  return Decode::Ok;
}

// ---- requests --------------------------------------------------------------

enum class Kind { Sites, VehicleKey, Entry, AddKey, AddSite, RemoveSite };

struct Request {
  Kind kind{Kind::Sites};
  std::string nonce;
  char domain{0};      // VehicleKey: 'v' vehicle security (VCSEC), 'i' infotainment
  uint8_t key[65]{};   // Entry / AddKey: the gateway's raw P-256 point
  Site site{};         // AddSite (all fields) / RemoveSite (key only)
  bool write() const { return kind == Kind::AddKey || kind == Kind::AddSite || kind == Kind::RemoveSite; }
};

inline bool raw_point(const std::string &hex_text, uint8_t (&out)[65]) {
  if (!cos::hex(hex_text, 130) || hex_text.compare(0, 2, "04") != 0) return false;
  for (size_t i = 0; i < 65; ++i) {
    auto nibble = [](char c) { return uint8_t(c <= '9' ? c - '0' : c - 'a' + 10); };
    out[i] = uint8_t(nibble(hex_text[2 * i]) << 4 | nibble(hex_text[2 * i + 1]));
  }
  return true;
}

inline bool coordinate(const std::string &s, int32_t limit, float &out) {
  if (s.empty() || s.size() > 10) return false;
  size_t i = s[0] == '-' ? 1 : 0;
  if (i == s.size() || (s.size() - i > 1 && s[i] == '0')) return false;
  int64_t v = 0;
  for (; i < s.size(); ++i) { if (s[i] < '0' || s[i] > '9') return false; v = v * 10 + (s[i] - '0'); }
  if (v > limit) return false;
  out = float(double(s[0] == '-' ? -v : v) / 1e5);
  return true;
}

// Reads:
//   "sit:<nonce>"               managed charging sites (CarServer 73)
//   "key:<nonce>:<v|i>"         vehicle key from session info (no BLE traffic)
//   "ent:<nonce>:<130 hex>"     whitelist entry for a key (VCSEC information request)
// Writes (each sent once, never retried):
//   "wla:<nonce>:<130 hex>"     add the key as ROLE_CHARGING_MANAGER; the car
//                               then waits for a key-card tap
//   "ste:<nonce>:<lat e5>:<lon e5>:<din>:<key text>"  add a managed site, read back
//   "rms:<nonce>:<key text>"    remove the site with that key, read back
inline bool parse_request(const std::string &text, Request &out) {
  if (text.size() < 36 || text[3] != ':' || text.size() > 255) return false;
  Request r;
  r.nonce = text.substr(4, 32);
  if (!cos::hex(r.nonce, 32)) return false;
  const std::string action = text.substr(0, 3);
  const std::string rest = text.size() > 37 ? text.substr(37) : std::string();
  if (text.size() > 36 && text[36] != ':') return false;
  if (action == "sit") {
    if (text.size() != 36) return false;
    r.kind = Kind::Sites;
  } else if (action == "key") {
    if (rest.size() != 1 || (rest[0] != 'v' && rest[0] != 'i')) return false;
    r.kind = Kind::VehicleKey;
    r.domain = rest[0];
  } else if (action == "ent" || action == "wla") {
    if (!raw_point(rest, r.key)) return false;
    r.kind = action == "ent" ? Kind::Entry : Kind::AddKey;
  } else if (action == "ste") {
    size_t a = rest.find(':'), b = a == std::string::npos ? a : rest.find(':', a + 1);
    size_t c = b == std::string::npos ? b : rest.find(':', b + 1);
    if (c == std::string::npos) return false;
    r.kind = Kind::AddSite;
    r.site.has_location = true;
    r.site.din = rest.substr(b + 1, c - b - 1);
    r.site.key = rest.substr(c + 1);
    if (!coordinate(rest.substr(0, a), 9000000, r.site.latitude)
        || !coordinate(rest.substr(a + 1, b - a - 1), 18000000, r.site.longitude)
        || r.site.key.find(':') != std::string::npos || !valid_site(r.site)) return false;
  } else if (action == "rms") {
    r.kind = Kind::RemoveSite;
    r.site.key = rest;
    if (rest.empty() || rest.size() > MAX_KEY_TEXT || classify_key(rest) == 'o') return false;
  } else {
    return false;
  }
  out = r;
  return true;
}

}}}  // namespace esphome::tesla_ble_vehicle::cos_pairing
