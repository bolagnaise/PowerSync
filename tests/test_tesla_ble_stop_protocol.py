"""Exercise the firmware's real protocol core without a vehicle or ESP32."""
from pathlib import Path
import subprocess


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
