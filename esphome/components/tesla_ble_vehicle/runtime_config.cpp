// SPDX-License-Identifier: AGPL-3.0-only
// PowerSync runtime configuration for prebuilt Tesla BLE bridge images.
//
// A prebuilt image is compiled with placeholder VIN/MAC substitutions. With
// `runtime_config: true` the component ignores the placeholders, loads a VIN
// and car BLE address saved in flash, and otherwise waits for a VIN to be
// entered (for example from Home Assistant). It then discovers the car's BLE
// address from the advertisement name Tesla derives from the VIN.
#include "tesla_ble_vehicle.h"
#include "esphome/core/log.h"
#include "esphome/core/preferences.h"
#include "esphome/core/helpers.h"
#include "mbedtls/sha1.h"
#include <cctype>
#include <cstring>

namespace esphome {
namespace tesla_ble_vehicle {

static const char *const RT_TAG = "tesla_ble_runtime";

namespace {
struct RuntimeConfigBlob {
  char vin[18];
  uint64_t mac;
};

ESPPreferenceObject &runtime_pref() {
  static ESPPreferenceObject pref =
      global_preferences->make_preference<RuntimeConfigBlob>(fnv1_hash("powersync_tesla_ble_runtime_v1"), true);
  return pref;
}

bool valid_vin(const std::string &vin) {
  if (vin.size() != 17) return false;
  for (char c : vin) {
    if (!std::isalnum(static_cast<unsigned char>(c))) return false;
  }
  return vin != "00000000000000000";
}

std::string advertisement_name(const char *vin) {
  unsigned char digest[20];
  if (mbedtls_sha1(reinterpret_cast<const unsigned char *>(vin), 17, digest) != 0) return "";
  char name[19];
  name[0] = 'S';
  for (int i = 0; i < 8; i++) snprintf(&name[1 + i * 2], 3, "%02x", digest[i]);
  name[17] = 'C';
  name[18] = '\0';
  return std::string(name);
}
}  // namespace

void TeslaBLEVehicle::load_runtime_config_() {
  if (!runtime_config_) return;
  // Placeholders compiled into the image are never a real car.
  recovery_vin_.clear();
  RuntimeConfigBlob blob{};
  if (!runtime_pref().load(&blob)) {
    ESP_LOGW(RT_TAG, "No Tesla VIN configured yet; set it from Home Assistant");
    return;
  }
  blob.vin[17] = '\0';
  if (!valid_vin(std::string(blob.vin))) {
    ESP_LOGW(RT_TAG, "Saved Tesla VIN is invalid; set it again");
    return;
  }
  std::memcpy(runtime_vin_buf_, blob.vin, sizeof(runtime_vin_buf_));
  this->set_vin(runtime_vin_buf_);
  runtime_expected_name_ = advertisement_name(runtime_vin_buf_);
  if (blob.mac != 0) {
    this->parent()->set_address(blob.mac);
    ESP_LOGI(RT_TAG, "Loaded Tesla VIN ...%s and car BLE address", runtime_vin_buf_ + 13);
  } else {
    runtime_discovering_ = true;
    ESP_LOGI(RT_TAG, "Loaded Tesla VIN ...%s; looking for the car (%s)", runtime_vin_buf_ + 13,
             runtime_expected_name_.c_str());
  }
}

bool TeslaBLEVehicle::set_runtime_vin(const std::string &input) {
  if (!runtime_config_) {
    ESP_LOGW(RT_TAG, "This bridge was built with a fixed VIN; runtime VIN ignored");
    return false;
  }
  std::string vin;
  for (char c : input) {
    if (!std::isspace(static_cast<unsigned char>(c))) vin += static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
  }
  if (!valid_vin(vin)) {
    ESP_LOGW(RT_TAG, "Rejected Tesla VIN: expected 17 letters or digits");
    return false;
  }
  if (vin == std::string(runtime_vin_buf_)) return true;
  RuntimeConfigBlob blob{};
  std::memcpy(blob.vin, vin.c_str(), 17);
  blob.vin[17] = '\0';
  blob.mac = 0;
  if (!runtime_pref().save(&blob) || !global_preferences->sync()) {
    ESP_LOGE(RT_TAG, "Could not save the Tesla VIN");
    return false;
  }
  std::memcpy(runtime_vin_buf_, blob.vin, sizeof(runtime_vin_buf_));
  this->set_vin(runtime_vin_buf_);
  runtime_expected_name_ = advertisement_name(runtime_vin_buf_);
  // A different car: forget the previous address and find the new one.
  this->parent()->disconnect();
  this->parent()->set_address(0);
  runtime_discovering_ = true;
  ESP_LOGI(RT_TAG, "Tesla VIN ...%s saved; looking for the car (%s)", runtime_vin_buf_ + 13,
           runtime_expected_name_.c_str());
  return true;
}

bool TeslaBLEVehicle::parse_device(const esp32_ble_tracker::ESPBTDevice &device) {
  if (!runtime_config_ || !runtime_discovering_ || runtime_expected_name_.empty()) return false;
  if (device.get_name() != runtime_expected_name_) return false;
  const uint64_t mac = device.address_uint64();
  RuntimeConfigBlob blob{};
  std::memcpy(blob.vin, runtime_vin_buf_, sizeof(blob.vin));
  blob.mac = mac;
  runtime_pref().save(&blob);
  global_preferences->sync();
  runtime_discovering_ = false;
  this->parent()->set_address(mac);
  ESP_LOGI(RT_TAG, "Found the car at %s; connecting", device.address_str().c_str());
  return false;
}

}  // namespace tesla_ble_vehicle
}  // namespace esphome
