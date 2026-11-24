// SPDX-License-Identifier: AGPL-3.0-only
// Tesla native Charge on Solar over BLE. Pure protocol helpers with no
// ESPHome, BLE or nanopb dependency so they can be host-tested.
//
// Wire layout (Teslemetry extended CarServer schema, absent from the pinned
// TeslaBLE nanopb headers, so encoded/decoded here by field number):
//   Action.vehicleAction = 2
//   VehicleAction.updateChargeOnSolarFeatureRequest = 74
//   VehicleAction.getChargeOnSolarFeatureRequest    = 75
//   Response.getChargeOnSolarFeatureResponse        = 15
//   {Update,Get}...{Request,Response}.charge_on_solar = 1
//   ChargeOnSolarFeature{bool enabled = 1; float lower = 2; float upper = 3}
// Encoding is proto3-canonical (default values omitted), byte-identical to
// protoc and to tesla-fleet-api's Bluetooth update_charge_on_solar.
#pragma once
#include <cmath>
#include <cstdint>
#include <cstddef>
#include <cstring>
#include <string>

namespace esphome { namespace tesla_ble_vehicle { namespace cos {

struct Settings {
  bool enabled{false};
  uint8_t lower{0};
  uint8_t upper{0};
  bool operator==(const Settings &o) const {
    return enabled == o.enabled && lower == o.lower && upper == o.upper;
  }
};

inline bool valid(const Settings &s) { return s.lower <= s.upper && s.upper <= 100; }

// ---- encoding --------------------------------------------------------------

struct Writer {
  uint8_t *out;
  size_t cap, len{0};
  bool ok{true};
  void byte(uint8_t b) { if (len < cap) out[len++] = b; else ok = false; }
  void varint(uint32_t v) {
    while (v >= 0x80) { byte(uint8_t(v) | 0x80); v >>= 7; }
    byte(uint8_t(v));
  }
  void tag(uint32_t field, uint8_t wire) { varint((field << 3) | wire); }
  void fixed32(float f) {
    uint32_t bits; std::memcpy(&bits, &f, 4);
    for (int i = 0; i < 4; ++i) byte(uint8_t(bits >> (8 * i)));
  }
  void bytes(uint32_t field, const uint8_t *data, size_t n) {
    tag(field, 2); varint(uint32_t(n));
    for (size_t i = 0; i < n; ++i) byte(data[i]);
  }
};

// Wrap an encoded VehicleAction oneof member as Action{vehicleAction{...}}.
inline size_t wrap_vehicle_action(uint32_t field, const uint8_t *member, size_t n,
                                  uint8_t *out, size_t cap) {
  uint8_t inner[32];
  Writer w{inner, sizeof(inner)};
  w.bytes(field, member, n);
  if (!w.ok) return 0;
  Writer a{out, cap};
  a.bytes(2, inner, w.len);
  return a.ok ? a.len : 0;
}

inline size_t encode_get(uint8_t *out, size_t cap) {
  return wrap_vehicle_action(75, nullptr, 0, out, cap);
}

inline size_t encode_set(const Settings &s, uint8_t *out, size_t cap) {
  if (!valid(s)) return 0;
  uint8_t feature[16];
  Writer f{feature, sizeof(feature)};
  if (s.enabled) { f.tag(1, 0); f.varint(1); }
  if (s.lower) { f.tag(2, 5); f.fixed32(float(s.lower)); }
  if (s.upper) { f.tag(3, 5); f.fixed32(float(s.upper)); }
  uint8_t request[24];
  Writer r{request, sizeof(request)};
  r.bytes(1, feature, f.len);
  if (!f.ok || !r.ok) return 0;
  return wrap_vehicle_action(74, request, r.len, out, cap);
}

// ---- decoding --------------------------------------------------------------

struct Reader {
  const uint8_t *p;
  size_t n, i{0};
  bool varint(uint64_t &v) {
    v = 0;
    for (int shift = 0; shift < 64; shift += 7) {
      if (i >= n) return false;
      uint8_t b = p[i++];
      v |= uint64_t(b & 0x7f) << shift;
      if (!(b & 0x80)) return true;
    }
    return false;
  }
  // Read one field header and its value span. LEN values return their
  // payload span; scalar values return the raw encoded span.
  bool field(uint32_t &number, uint8_t &wire, const uint8_t *&value, size_t &size) {
    uint64_t key;
    if (!varint(key) || (key >> 3) == 0 || (key >> 3) > 0x1fffffff) return false;
    number = uint32_t(key >> 3);
    wire = uint8_t(key & 7);
    size_t start = i;
    switch (wire) {
      case 0: { uint64_t ignored; if (!varint(ignored)) return false; value = p + start; size = i - start; return true; }
      case 1: if (n - i < 8) return false; value = p + i; size = 8; i += 8; return true;
      case 5: if (n - i < 4) return false; value = p + i; size = 4; i += 4; return true;
      case 2: {
        uint64_t length;
        if (!varint(length) || length > n - i) return false;
        value = p + i; size = size_t(length); i += size; return true;
      }
      default: return false;  // groups are not used by this protocol
    }
  }
  bool done() const { return i == n; }
};

enum class Decode { Ok, Absent, Malformed };

// Limits arrive as floats. Only whole percentages are accepted; anything
// else is reported as malformed rather than silently rounded.
inline bool limit(const uint8_t *raw, uint8_t &out) {
  uint32_t bits = uint32_t(raw[0]) | uint32_t(raw[1]) << 8 | uint32_t(raw[2]) << 16 | uint32_t(raw[3]) << 24;
  float f; std::memcpy(&f, &bits, 4);
  if (!std::isfinite(f) || f < 0 || f > 100 || std::floor(f) != f) return false;
  out = uint8_t(f);
  return true;
}

inline Decode decode_feature(const uint8_t *data, size_t len, Settings &out) {
  Settings s{};
  Reader r{data, len};
  while (!r.done()) {
    uint32_t number; uint8_t wire; const uint8_t *v; size_t size;
    if (!r.field(number, wire, v, size)) return Decode::Malformed;
    if (number == 1 && wire == 0) {
      Reader b{v, size}; uint64_t flag;
      if (!b.varint(flag)) return Decode::Malformed;
      s.enabled = flag != 0;
    } else if ((number == 2 || number == 3) && wire == 5) {
      if (!limit(v, number == 2 ? s.lower : s.upper)) return Decode::Malformed;
    } else if (number <= 3) {
      return Decode::Malformed;  // a known field with the wrong wire type
    }
  }
  out = s;
  return Decode::Ok;
}

// Find Response.getChargeOnSolarFeatureResponse(15).charge_on_solar(1).
inline Decode decode_response(const uint8_t *data, size_t len, Settings &out) {
  Reader r{data, len};
  bool found = false;
  Settings s{};
  while (!r.done()) {
    uint32_t number; uint8_t wire; const uint8_t *v; size_t size;
    if (!r.field(number, wire, v, size)) return Decode::Malformed;
    if (number != 15) continue;
    if (wire != 2) return Decode::Malformed;
    Reader inner{v, size};
    s = Settings{};  // an empty response message means every field is default
    while (!inner.done()) {
      uint32_t n2; uint8_t w2; const uint8_t *v2; size_t s2;
      if (!inner.field(n2, w2, v2, s2)) return Decode::Malformed;
      if (n2 != 1) continue;
      if (w2 != 2 || decode_feature(v2, s2, s) != Decode::Ok) return Decode::Malformed;
    }
    found = true;
  }
  if (!found) return Decode::Absent;
  out = s;
  return Decode::Ok;
}

// ---- requests --------------------------------------------------------------

inline bool hex(const std::string &s, size_t length) {
  if (s.size() != length) return false;
  for (char c : s) if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
  return true;
}

inline bool percent(const std::string &s, uint8_t &out) {
  if (s.empty() || s.size() > 3) return false;
  unsigned v = 0;
  for (char c : s) { if (c < '0' || c > '9') return false; v = v * 10 + unsigned(c - '0'); }
  if (v > 100 || (s.size() > 1 && s[0] == '0')) return false;
  out = uint8_t(v);
  return true;
}

struct Request {
  bool write{false};
  std::string nonce;
  Settings settings{};
};

// "get:<32 hex>" or "set:<32 hex>:<0|1>:<lower>:<upper>"
inline bool parse_request(const std::string &text, Request &out) {
  Request r;
  if (text.size() < 36 || text[3] != ':') return false;
  const std::string action = text.substr(0, 3);
  r.nonce = text.substr(4, 32);
  if (!hex(r.nonce, 32)) return false;
  if (action == "get") {
    if (text.size() != 36) return false;
  } else if (action == "set") {
    if (text.size() < 42 || text[36] != ':' || (text[37] != '0' && text[37] != '1') || text[38] != ':') return false;
    const std::string limits = text.substr(39);
    auto colon = limits.find(':');
    if (colon == std::string::npos) return false;
    r.write = true;
    r.settings.enabled = text[37] == '1';
    if (!percent(limits.substr(0, colon), r.settings.lower)
        || !percent(limits.substr(colon + 1), r.settings.upper) || !valid(r.settings)) return false;
  } else {
    return false;
  }
  out = r;
  return true;
}

// ---- request state ---------------------------------------------------------

// Phases: i idle, w working, k confirmed readback, f failed with no change
// applied, u uncertain (a write may have been applied but was not confirmed).
class Protocol {
 public:
  static constexpr uint32_t TIMEOUT_MS = 75000;
  static constexpr size_t MAX_SENT = 8;
  struct Sent { char uuid[33]{}; uint8_t hash[17]{}; uint8_t hash_len{0}; };

  char phase{'i'};
  std::string nonce, error;
  Request request{};
  Settings observed{};
  bool reading_back{false}, write_sent{false}, write_acked{false};
  uint32_t started_ms{0};
  Sent sent[MAX_SENT]{};
  size_t sent_count{0};

  bool busy() const { return phase == 'w'; }

  bool begin(const Request &r, uint32_t now) {
    if (busy() || r.nonce == nonce) return false;  // one at a time, no replays
    request = r; nonce = r.nonce; error.clear();
    phase = 'w'; started_ms = now;
    reading_back = !r.write; write_sent = write_acked = false;
    sent_count = 0;
    return true;
  }

  // A refused request still answers its own nonce so the caller need not
  // wait for a timeout. It never displaces an in-flight request.
  bool refuse(const Request &r, const char *why) {
    if (busy() || r.nonce == nonce) return false;
    request = r; nonce = r.nonce; error = why;
    phase = 'f'; write_sent = write_acked = false; sent_count = 0;
    return true;
  }

  bool record(const char *uuid_hex, const uint8_t *hash, size_t hash_len) {
    if (!busy() || !hex(uuid_hex, 32) || hash_len > sizeof(Sent::hash)) return false;
    Sent &slot = sent[sent_count % MAX_SENT];
    std::memcpy(slot.uuid, uuid_hex, 33);
    std::memcpy(slot.hash, hash, hash_len);
    slot.hash_len = uint8_t(hash_len);
    ++sent_count;
    if (!reading_back) write_sent = true;
    return true;
  }

  const Sent *find(const std::string &uuid_hex) const {
    if (!busy()) return nullptr;
    size_t count = sent_count < MAX_SENT ? sent_count : MAX_SENT;
    for (size_t i = 0; i < count; ++i)
      if (uuid_hex == sent[i].uuid) return &sent[i];
    return nullptr;
  }

  // The write was accepted. Readback is a separate request with its own UUIDs.
  void acknowledged() { write_acked = true; reading_back = true; sent_count = 0; }

  void confirmed(const Settings &s) { observed = s; phase = 'k'; }

  // Anything after a write left the car fails as uncertain, unless the car
  // explicitly rejected that write (rejected=true) and nothing was acked.
  void fail(const std::string &why, bool rejected = false) {
    // A rejection proves nothing changed only if exactly one write frame left
    // the bridge; an earlier unanswered retry may still have been applied.
    error = why;
    const bool unapplied = rejected && sent_count == 1;
    phase = (write_acked || (write_sent && !unapplied)) ? 'u' : 'f';
  }

  bool expired(uint32_t now) const { return busy() && uint32_t(now - started_ms) >= TIMEOUT_MS; }
};

}}}  // namespace esphome::tesla_ble_vehicle::cos
