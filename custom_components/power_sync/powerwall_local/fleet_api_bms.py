"""DeviceControllerQuery envelope encoder + response extractor for Fleet API BMS reads.

Hand-rolled protobuf wire encoder. tesla_protocol.energy_device.transport_pb2 has field
16 bound to GraphQLMessages; this query needs field 16 = QueryType, so we can't reuse the
compiled module. ~40 lines of varint + length-delimited encoding mirrors the exact same
wire format as powersync-cc/worker/src/protobuf.ts buildDeviceControllerQueryEnvelope.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Wire types
_WT_VARINT = 0
_WT_LEN = 2


def _encode_varint(v: int) -> bytes:
    out = bytearray()
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v & 0x7F)
    return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _encode_varint((field << 3) | wire)


def _field_varint(field: int, value: int) -> bytes:
    return _tag(field, _WT_VARINT) + _encode_varint(value)


def _field_bytes(field: int, value: bytes) -> bytes:
    return _tag(field, _WT_LEN) + _encode_varint(len(value)) + value


def _field_string(field: int, value: str) -> bytes:
    return _field_bytes(field, value.encode("utf-8"))


# ── DeviceControllerQuery constants ──────────────────────────────────────────
# Verbatim from powersync-cc/worker/src/protobuf.ts:348-361.
# CRITICAL: The ECDSA code blob signs the canonical QUERY TEXT bytes. Do not
# reformat, add whitespace, or change encoding of DEVICE_CONTROLLER_QUERY.

# Pre-signed 138-byte ECDSA literal — same bytes on every site/DIN.
DEVICE_CONTROLLER_QUERY_CODE_B64 = "MIGHAkIBQZUS40LRyhrTAPZ9C0VAL5qfwA0GJawsDmohKQpk7+Y3i69i1/gmCy7BrNkhH9aD/2tJbfNcStjuaVRZ3n/FeFICQR1DA0j7OCKw5NYY3hHENbKpVkKmSo8InbqG8SBXzUqMAioFEst7PJvIZ8mdOYtSs4m48fEPDhZF7de/1SYpki4S"  # noqa: E501

# Verbatim from pypowerwall/tedapi/__init__.py:746 (via worker protobuf.ts:354-355).
# Single unbroken string — any whitespace change invalidates the ECDSA signature.
DEVICE_CONTROLLER_QUERY = "query DeviceControllerQuery($msaComp:ComponentFilter$msaSignals:[String!]){control{systemStatus{nominalFullPackEnergyWh nominalEnergyRemainingWh}islanding{customerIslandMode contactorClosed microGridOK gridOK disableReasons}meterAggregates{location realPowerW}alerts{active}siteShutdown{isShutDown reasons}batteryBlocks{din disableReasons}pvInverters{din disableReasons}}system{time supportMode{remoteService{isEnabled expiryTime sessionId}}sitemanagerStatus{isRunning}updateUrgencyCheck{urgency version{version gitHash}timestamp}}neurio{isDetectingWiredMeters readings{firmwareVersion serial dataRead{voltageV realPowerW reactivePowerVAR currentA}timestamp}pairings{serial shortId status errors macAddress hostname isWired modbusPort modbusId lastUpdateTimestamp}}teslaRemoteMeter{meters{din reading{timestamp firmwareVersion ctReadings{voltageV realPowerW reactivePowerVAR energyExportedWs energyImportedWs currentA}}firmwareUpdate{updating numSteps currentStep currentStepProgress progress}}detectedWired{din serialPort}}pw3Can{firmwareUpdate{isUpdating progress{updating numSteps currentStep currentStepProgress progress}}enumeration{inProgress}}esCan{bus{PVAC{packagePartNumber packageSerialNumber subPackagePartNumber subPackageSerialNumber PVAC_Status{isMIA PVAC_Pout PVAC_State PVAC_Vout PVAC_Fout}PVAC_InfoMsg{PVAC_appGitHash}PVAC_Logging{isMIA PVAC_PVCurrent_A PVAC_PVCurrent_B PVAC_PVCurrent_C PVAC_PVCurrent_D PVAC_PVMeasuredVoltage_A PVAC_PVMeasuredVoltage_B PVAC_PVMeasuredVoltage_C PVAC_PVMeasuredVoltage_D PVAC_VL1Ground PVAC_VL2Ground}alerts{isComplete isMIA active}}PINV{PINV_Status{isMIA PINV_Fout PINV_Pout PINV_Vout PINV_State PINV_GridState}PINV_AcMeasurements{isMIA PINV_VSplit1 PINV_VSplit2}PINV_PowerCapability{isComplete isMIA PINV_Pnom}alerts{isComplete isMIA active}}PVS{PVS_Status{isMIA PVS_State PVS_vLL PVS_StringA_Connected PVS_StringB_Connected PVS_StringC_Connected PVS_StringD_Connected PVS_SelfTestState}PVS_Logging{PVS_numStringsLockoutBits PVS_sbsComplete}alerts{isComplete isMIA active}}THC{packagePartNumber packageSerialNumber THC_InfoMsg{isComplete isMIA THC_appGitHash}THC_Logging{THC_LOG_PW_2_0_EnableLineState}}POD{POD_EnergyStatus{isMIA POD_nom_energy_remaining POD_nom_full_pack_energy}POD_InfoMsg{POD_appGitHash}}SYNC{packagePartNumber packageSerialNumber SYNC_InfoMsg{isMIA SYNC_appGitHash SYNC_assemblyId}METER_X_AcMeasurements{isMIA isComplete METER_X_CTA_InstRealPower METER_X_CTA_InstReactivePower METER_X_CTA_I METER_X_VL1N METER_X_CTB_InstRealPower METER_X_CTB_InstReactivePower METER_X_CTB_I METER_X_VL2N METER_X_CTC_InstRealPower METER_X_CTC_InstReactivePower METER_X_CTC_I METER_X_VL3N}METER_Y_AcMeasurements{isMIA isComplete METER_Y_CTA_InstRealPower METER_Y_CTA_InstReactivePower METER_Y_CTA_I METER_Y_VL1N METER_Y_CTB_InstRealPower METER_Y_CTB_InstReactivePower METER_Y_CTB_I METER_Y_VL2N METER_Y_CTC_InstRealPower METER_Y_CTC_InstReactivePower METER_Y_CTC_I METER_Y_VL3N}}ISLANDER{ISLAND_GridConnection{ISLAND_GridConnected isComplete}ISLAND_AcMeasurements{ISLAND_VL1N_Main ISLAND_FreqL1_Main ISLAND_VL2N_Main ISLAND_FreqL2_Main ISLAND_VL3N_Main ISLAND_FreqL3_Main ISLAND_VL1N_Load ISLAND_FreqL1_Load ISLAND_VL2N_Load ISLAND_FreqL2_Load ISLAND_VL3N_Load ISLAND_FreqL3_Load ISLAND_GridState isComplete isMIA}}}enumeration{inProgress numACPW numPVI}firmwareUpdate{isUpdating powerwalls{updating numSteps currentStep currentStepProgress progress}msa{updating numSteps currentStep currentStepProgress progress}msa1{updating numSteps currentStep currentStepProgress progress}sync{updating numSteps currentStep currentStepProgress progress}pvInverters{updating numSteps currentStep currentStepProgress progress}}phaseDetection{inProgress lastUpdateTimestamp powerwalls{din progress phase}}inverterSelfTests{isRunning isCanceled pinvSelfTestsResults{din overall{status test summary setMagnitude setTime tripMagnitude tripTime accuracyMagnitude accuracyTime currentMagnitude timestamp lastError}testResults{status test summary setMagnitude setTime tripMagnitude tripTime accuracyMagnitude accuracyTime currentMagnitude timestamp lastError}}}}components{msa:components(filter:$msaComp){partNumber serialNumber signals(names:$msaSignals){name value textValue boolValue timestamp}activeAlerts{name}}}ieee20305{longFormDeviceID polledResources{url name pollRateSeconds lastPolledTimestamp}controls{defaultControl{mRID setGradW opModEnergize opModMaxLimW opModImpLimW opModExpLimW opModGenLimW opModLoadLimW}activeControls{opModEnergize opModMaxLimW opModImpLimW opModExpLimW opModGenLimW opModLoadLimW}}registration{dateTimeRegistered pin}}}"  # noqa: E501

# Variables extend the base pypowerwall query with PW3BMS component type so that the
# response includes per-pack BMS_nominalFullPackEnergy and BMS_nominalEnergyRemaining.
# Verbatim from protobuf.ts:360-361 — escape sequences preserved.
DEVICE_CONTROLLER_QUERY_VARIABLES = '{"msaComp":{"types" :["PVS","PVAC", "TESYNC", "TEPINV", "TETHC", "STSTSM",  "TEMSA", "TEPINV", "PW3BMS" ]},\n\t"msaSignals":[\n\t"MSA_pcbaId",\n\t"MSA_usageId",\n\t"MSA_appGitHash",\n\t"PVAC_Fan_Speed_Actual_RPM",\n\t"PVAC_Fan_Speed_Target_RPM",\n\t"MSA_HeatingRateOccurred",\n\t"THC_AmbientTemp",\n\t"METER_Z_CTA_InstRealPower",\n\t"METER_Z_CTA_InstReactivePower",\n\t"METER_Z_CTA_I",\n\t"METER_Z_VL1G",\n\t"METER_Z_CTB_InstRealPower",\n\t"METER_Z_CTB_InstReactivePower",\n\t"METER_Z_CTB_I",\n\t"METER_Z_VL2G",\n\t"METER_Z_CTC_InstRealPower",\n\t"METER_Z_CTC_InstReactivePower",\n\t"METER_Z_CTC_I",\n\t"METER_Z_VL3G",\n\t"METER_Z_LifetimeEnergyExport",\n\t"METER_Z_LifetimeEnergyImport",\n\t"BMS_nominalFullPackEnergy",\n\t"BMS_nominalEnergyRemaining"]}'  # noqa: E501


PW3_COMPONENTS_QUERY_CODE_B64 = "MIGIAkIAuHHsPqNt1XD5U6uZ5n46Go5+orOHD0y7T4N1objbd5vsvqZosqOtsQeCRL/reBNPEsOOtAgtJ2k28D5Cn57EpG0CQgETUGsbKb+e4lK0p2ewdR4T9jLsousdkZwXpdFK4uUfcbJP5DYt681tMZ96YWkw4YdDNMfAWn9AaN3XEzmqZgqGVw=="  # noqa: E501

PW3_COMPONENTS_QUERY = (
    " query ComponentsQuery (\n  $pchComponentsFilter: ComponentFilter,\n  $pchSignalNames: [String!],\n  "
    "$pwsComponentsFilter: ComponentFilter,\n  $pwsSignalNames: [String!],\n  $bmsComponentsFilter: ComponentFilter,\n  "
    "$bmsSignalNames: [String!],\n  $hvpComponentsFilter: ComponentFilter,\n  $hvpSignalNames: [String!],\n  "
    "$baggrComponentsFilter: ComponentFilter,\n  $baggrSignalNames: [String!],\n  ) {\n  # TODO STST-57686: "
    "Introduce GraphQL fragments to shorten\n  pw3Can {\n    firmwareUpdate {\n      isUpdating\n      progress {\n         "
    "updating\n         numSteps\n         currentStep\n         currentStepProgress\n         progress\n      }\n    }\n  }\n  "
    "components {\n    pws: components(filter: $pwsComponentsFilter) {\n      signals(names: $pwsSignalNames) {\n        "
    "name\n        value\n        textValue\n        boolValue\n        timestamp\n      }\n      activeAlerts {\n        "
    "name\n      }\n    }\n    pch: components(filter: $pchComponentsFilter) {\n      signals(names: $pchSignalNames) {\n        "
    "name\n        value\n        textValue\n        boolValue\n        timestamp\n      }\n      activeAlerts {\n        "
    "name\n      }\n    }\n    bms: components(filter: $bmsComponentsFilter) {\n      signals(names: $bmsSignalNames) {\n        "
    "name\n        value\n        textValue\n        boolValue\n        timestamp\n      }\n      activeAlerts {\n        "
    "name\n      }\n    }\n    hvp: components(filter: $hvpComponentsFilter) {\n      partNumber\n      serialNumber\n      "
    "signals(names: $hvpSignalNames) {\n        name\n        value\n        textValue\n        boolValue\n        "
    "timestamp\n      }\n      activeAlerts {\n        name\n      }\n    }\n    baggr: components(filter: $baggrComponentsFilter) "
    "{\n      signals(names: $baggrSignalNames) {\n        name\n        value\n        textValue\n        boolValue\n        "
    "timestamp\n      }\n      activeAlerts {\n        name\n      }\n    }\n  }\n}\n"
)

PW3_COMPONENTS_QUERY_VARIABLES = '{"pwsComponentsFilter":{"types":["PW3SAF"]},"pwsSignalNames":["PWS_SelfTest","PWS_PeImpTestState","PWS_PvIsoTestState","PWS_RelaySelfTest_State","PWS_MciTestState","PWS_appGitHash","PWS_ProdSwitch_State"],"pchComponentsFilter":{"types":["PCH"]},"pchSignalNames":["PCH_State","PCH_PvState_A","PCH_PvState_B","PCH_PvState_C","PCH_PvState_D","PCH_PvState_E","PCH_PvState_F","PCH_AcFrequency","PCH_AcVoltageAB","PCH_AcVoltageAN","PCH_AcVoltageBN","PCH_packagePartNumber_1_7","PCH_packagePartNumber_8_14","PCH_packagePartNumber_15_20","PCH_packageSerialNumber_1_7","PCH_packageSerialNumber_8_14","PCH_PvVoltageA","PCH_PvVoltageB","PCH_PvVoltageC","PCH_PvVoltageD","PCH_PvVoltageE","PCH_PvVoltageF","PCH_PvCurrentA","PCH_PvCurrentB","PCH_PvCurrentC","PCH_PvCurrentD","PCH_PvCurrentE","PCH_PvCurrentF","PCH_BatteryPower","PCH_AcRealPowerAB","PCH_SlowPvPowerSum","PCH_AcMode","PCH_AcFrequency","PCH_DcdcState_A","PCH_DcdcState_B","PCH_appGitHash"],"bmsComponentsFilter":{"types":["PW3BMS"]},"bmsSignalNames":["BMS_nominalEnergyRemaining","BMS_nominalFullPackEnergy","BMS_appGitHash"],"hvpComponentsFilter":{"types":["PW3HVP"]},"hvpSignalNames":["HVP_State","HVP_appGitHash"],"baggrComponentsFilter":{"types":["BAGGR"]},"baggrSignalNames":["BAGGR_State","BAGGR_OperationRequest","BAGGR_NumBatteriesConnected","BAGGR_NumBatteriesPresent","BAGGR_NumBatteriesExpected","BAGGR_LOG_BattConnectionStatus0","BAGGR_LOG_BattConnectionStatus1","BAGGR_LOG_BattConnectionStatus2","BAGGR_LOG_BattConnectionStatus3"]}'  # noqa: E501


def _build_old_tedapi_query_envelope(
    din: str,
    query: str,
    code_b64: str,
    variables: str,
) -> bytes:
    """Build a MessageEnvelope for a signed old-TEDAPI GraphQL query."""
    code_bytes = base64.b64decode(code_b64)

    sender = _field_varint(3, 1)          # Participant.local = 1
    recipient = _field_string(1, din)     # Participant.din
    query_payload = _field_varint(1, 1) + _field_string(2, query)
    query_b = _field_string(1, variables)
    query_send = (
        _field_varint(1, 2)
        + _field_bytes(2, query_payload)
        + _field_bytes(3, code_bytes)
        + _field_bytes(4, query_b)
    )
    query_type = _field_bytes(1, query_send)

    return (
        _field_varint(1, 1)
        + _field_bytes(2, sender)
        + _field_bytes(3, recipient)
        + _field_bytes(16, query_type)
    )


def build_device_controller_query_envelope(din: str) -> bytes:
    """Build a MessageEnvelope for DeviceControllerQuery (tedapi.proto wire format).

    Wire layout:
      deliveryChannel = 1  (varint)
      sender          = 2  (Participant.local = 1)
      recipient       = 3  (Participant.din = <din>)
      payload         = 16 (QueryType.send(1) = PayloadQuerySend{
                              num(1)=2, payload(2)={value(1)=1, text(2)=<query>},
                              code(3)=<138-byte ECDSA>, b(4)={value(1)=<variables>}
                            })
    """
    return _build_old_tedapi_query_envelope(
        din,
        DEVICE_CONTROLLER_QUERY,
        DEVICE_CONTROLLER_QUERY_CODE_B64,
        DEVICE_CONTROLLER_QUERY_VARIABLES,
    )


def build_pw3_components_query_envelope(din: str) -> bytes:
    """Build a MessageEnvelope for PW3 ComponentsQuery PCH string telemetry."""
    return _build_old_tedapi_query_envelope(
        din,
        PW3_COMPONENTS_QUERY,
        PW3_COMPONENTS_QUERY_CODE_B64,
        PW3_COMPONENTS_QUERY_VARIABLES,
    )


# ── Response decoder ──────────────────────────────────────────────────────────

def _decode_varint(buf: bytes, pos: int) -> tuple[int, int]:
    v, shift = 0, 0
    while True:
        b = buf[pos]
        pos += 1
        v |= (b & 0x7F) << shift
        if not (b & 0x80):
            return v, pos
        shift += 7


def _find_field(buf: bytes, field_num: int) -> bytes | None:
    """Return the first length-delimited field matching field_num, or None."""
    pos = 0
    while pos < len(buf):
        tag, pos = _decode_varint(buf, pos)
        wire = tag & 7
        fn = tag >> 3
        if wire == _WT_VARINT:
            _, pos = _decode_varint(buf, pos)
        elif wire == _WT_LEN:
            ln, pos = _decode_varint(buf, pos)
            val = buf[pos:pos + ln]
            pos += ln
            if fn == field_num:
                return val
        elif wire == 1:
            pos += 8  # fixed64
        elif wire == 5:
            pos += 4  # fixed32
        else:
            return None  # unknown wire type — bail
    return None


def extract_query_recv_text(envelope_bytes: bytes) -> str | None:
    """Extract the JSON text from a DeviceControllerQuery response envelope.

    Response path: payload(16) → QueryType.recv(2) → PayloadString.text(2)
    """
    query_type = _find_field(envelope_bytes, 16)
    if query_type is None:
        return None
    recv = _find_field(query_type, 2)
    if recv is None:
        return None
    text_bytes = _find_field(recv, 2)
    return text_bytes.decode("utf-8") if text_bytes else None


def parse_device_controller_response(envelope_bytes: bytes) -> dict | None:
    """Extract and JSON-parse the DeviceControllerQuery response from envelope bytes."""
    text = extract_query_recv_text(envelope_bytes)
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception as err:
        _LOGGER.warning("fleet_api_bms: JSON parse error: %s", err)
        return None


# ── Solar string normalisers ───────────────────────────────────────────────────

_PW3_STRING_LETTERS = ("A", "B", "C", "D", "E", "F")
_LEGACY_STRING_LETTERS = ("A", "B", "C", "D")


def _number_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _positive_number_or_none(value: Any) -> float | None:
    number = _number_or_none(value)
    return number if number is not None and number > 0 else None


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _power_or_none(voltage_v: float | None, current_a: float | None) -> float | None:
    return voltage_v * current_a if voltage_v is not None and current_a is not None else None


def _connected_from_state(state: str | None) -> bool | None:
    if not state:
        return None
    normalized = state.lower()
    if "active" in normalized:
        return True
    if "disabled" in normalized or "standby" in normalized:
        return False
    return None


def _has_useful_string_data(reading: dict[str, Any]) -> bool:
    return (
        (reading.get("voltage_v") is not None and reading["voltage_v"] > 0)
        or (reading.get("current_a") is not None and reading["current_a"] > 0)
        or (reading.get("power_w") is not None and reading["power_w"] > 0)
        or reading.get("connected") is True
        or bool(reading.get("state") and "active" in str(reading["state"]).lower())
    )


def _build_string_groups(strings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_device: dict[str, list[dict[str, Any]]] = {}
    for reading in strings:
        device_key = reading.get("device_id") or "gateway"
        by_device.setdefault(device_key, []).append(reading)

    groups: list[dict[str, Any]] = []
    for device_id, readings in by_device.items():
        by_mppt = {reading.get("mppt"): reading for reading in readings}
        for offset in range(0, len(_PW3_STRING_LETTERS), 2):
            pair = [
                reading
                for letter in _PW3_STRING_LETTERS[offset:offset + 2]
                if (reading := by_mppt.get(letter)) is not None
            ]
            if not pair:
                continue
            total_power = sum(reading.get("power_w") or 0 for reading in pair)
            connected_values = [
                reading.get("connected")
                for reading in pair
                if reading.get("connected") is not None
            ]
            groups.append({
                "id": f"{device_id}:{'+'.join(str(reading.get('mppt')) for reading in pair)}",
                "label": (
                    f"MPPT {pair[0].get('mppt')}+{pair[1].get('mppt')}"
                    if len(pair) == 2
                    else f"MPPT {pair[0].get('mppt')}"
                ),
                "string_ids": [reading["id"] for reading in pair],
                "voltage_v": [reading.get("voltage_v") for reading in pair],
                "total_power_w": total_power if total_power > 0 else None,
                "connected": any(connected_values) if connected_values else None,
            })
    return groups


def normalize_pw3_components_strings(data: dict[str, Any]) -> dict[str, Any] | None:
    """Extract PW3 PCH A-F string voltage/current readings from ComponentsQuery."""
    components = data.get("components") or {}
    pch = components.get("pch") if isinstance(components, dict) else None
    if not isinstance(pch, list) or not pch:
        return None

    strings: list[dict[str, Any]] = []
    for device_index, component in enumerate(pch):
        if not isinstance(component, dict):
            continue
        signals = component.get("signals") or []
        by_name = {
            signal.get("name"): signal
            for signal in signals
            if isinstance(signal, dict) and isinstance(signal.get("name"), str)
        }
        device_id = component.get("serialNumber") or (
            f"pch-{device_index + 1}" if len(pch) > 1 else None
        )

        for letter in _PW3_STRING_LETTERS:
            state = _string_or_none((by_name.get(f"PCH_PvState_{letter}") or {}).get("textValue"))
            voltage_v = _number_or_none((by_name.get(f"PCH_PvVoltage{letter}") or {}).get("value"))
            current_a = _positive_number_or_none((by_name.get(f"PCH_PvCurrent{letter}") or {}).get("value"))
            reading = {
                "id": f"{device_id or 'pch'}:{letter}",
                "label": f"{device_index + 1}{letter}" if len(pch) > 1 else letter,
                "device_id": device_id,
                "mppt": letter,
                "voltage_v": voltage_v,
                "current_a": current_a,
                "power_w": _power_or_none(voltage_v, current_a),
                "state": state,
                "connected": _connected_from_state(state),
            }
            if _has_useful_string_data(reading):
                strings.append(reading)

    if not strings:
        return None
    if not any(
        reading.get("connected") is True
        or (reading.get("current_a") is not None and reading["current_a"] > 0)
        or (reading.get("power_w") is not None and reading["power_w"] > 0)
        for reading in strings
    ):
        return None

    return {
        "source": "pw3_components",
        "strings": strings,
        "groups": _build_string_groups(strings),
    }


def normalize_legacy_pvac_strings(data: dict[str, Any]) -> dict[str, Any] | None:
    """Extract Powerwall+/PW2 PVAC/PVS string voltage/current readings."""
    es_can = data.get("esCan") or {}
    bus = es_can.get("bus") if isinstance(es_can, dict) else None
    if not isinstance(bus, dict):
        return None
    pvac = bus.get("PVAC")
    if not isinstance(pvac, list) or not pvac:
        return None
    pvs = bus.get("PVS") if isinstance(bus.get("PVS"), list) else []

    strings: list[dict[str, Any]] = []
    for device_index, device in enumerate(pvac):
        if not isinstance(device, dict):
            continue
        logging_data = device.get("PVAC_Logging")
        if not isinstance(logging_data, dict):
            continue
        status = device.get("PVAC_Status") if isinstance(device.get("PVAC_Status"), dict) else {}
        pvs_device = pvs[device_index] if device_index < len(pvs) and isinstance(pvs[device_index], dict) else {}
        pvs_status = pvs_device.get("PVS_Status") if isinstance(pvs_device.get("PVS_Status"), dict) else {}
        serial = _string_or_none(device.get("packageSerialNumber")) or (
            f"pvac-{device_index + 1}" if len(pvac) > 1 else None
        )

        for letter in _LEGACY_STRING_LETTERS:
            voltage_v = _number_or_none(logging_data.get(f"PVAC_PVMeasuredVoltage_{letter}"))
            current_a = _positive_number_or_none(logging_data.get(f"PVAC_PVCurrent_{letter}"))
            connected = _bool_or_none(pvs_status.get(f"PVS_String{letter}_Connected"))
            state = (
                _string_or_none(status.get("PVAC_State"))
                if connected is None
                else "PV_Active" if connected else "PV_Disabled"
            )
            reading = {
                "id": f"{serial or 'pvac'}:{letter}",
                "label": f"{device_index + 1}{letter}" if len(pvac) > 1 else letter,
                "device_id": serial,
                "mppt": letter,
                "voltage_v": voltage_v,
                "current_a": current_a,
                "power_w": _power_or_none(voltage_v, current_a),
                "state": state,
                "connected": connected,
            }
            if _has_useful_string_data(reading):
                strings.append(reading)

    if not strings:
        return None
    return {
        "source": "legacy_pvac",
        "strings": strings,
        "groups": _build_string_groups(strings),
    }


# ── Signing helper (no SSL context, no local gateway connection) ──────────────

_SIGNATURE_TYPE_RSA = 7
_DOMAIN_ENERGY_DEVICE = 7
_TAG_END = 0xFF


def build_signed_routable_message(
    envelope_bytes: bytes,
    din: str,
    private_key_pem: bytes,
    *,
    ttl_seconds: int = 300,
) -> bytes:
    """Sign a DeviceControllerQuery envelope and return a serialised RoutableMessage.

    This is the Fleet-API-relay equivalent of TEDAPIv1rTransport.build_signed_bytes(),
    but it avoids constructing a TEDAPIv1rTransport instance (which creates an SSL
    context synchronously and should not be called from the HA event loop directly).

    The returned bytes are base64-encoded and sent as ``routable_message`` in the
    ``device_command`` Fleet API call.
    """
    import math
    import struct
    import time
    import uuid as _uuid

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    from tesla_protocol.energy_device import signed_message_pb2

    private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    public_key_der = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.PKCS1,
    )

    routable = signed_message_pb2.RoutableMessage()
    routable.to_destination.domain = signed_message_pb2.DOMAIN_ENERGY_DEVICE
    routable.protobuf_message_as_bytes = envelope_bytes
    routable.uuid = str(_uuid.uuid4()).encode()

    expires_at = math.ceil(time.time()) + ttl_seconds

    def _tlv(tag: int, value: bytes) -> bytes:
        return bytes([tag, len(value)]) + value

    tlv_payload = b"".join([
        _tlv(0, bytes([_SIGNATURE_TYPE_RSA])),
        _tlv(1, bytes([_DOMAIN_ENERGY_DEVICE])),
        _tlv(2, din.encode()),
        _tlv(4, struct.pack(">I", expires_at)),
        bytes([_TAG_END]),
        routable.protobuf_message_as_bytes,
    ])

    signature = private_key.sign(
        data=tlv_payload,
        padding=padding.PKCS1v15(),
        algorithm=hashes.SHA512(),
    )

    routable.signature_data.signer_identity.public_key = public_key_der
    routable.signature_data.rsa_data.expires_at = expires_at
    routable.signature_data.rsa_data.signature = signature

    return routable.SerializeToString()
