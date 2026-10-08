# Vendor response fixtures

Sample PDU responses used by the driver unit tests. None were captured by us from real
hardware (no units were available); each comes from vendor-published material or from
other open-source projects that captured it from real units.
When a real unit becomes available, replace these with captured output and re-run the
tests. That is the real check that the drivers match the hardware.

| File | Origin | Verbatim? |
|---|---|---|
| `apc/olstatus_all.txt`, `apc/olstatus_3_off.txt` | APC NMC CLI `olStatus` output format (`E000: Success` + ` <n>: <name>: On/Off` lines) from APC Switched Rack PDU user guides | Format only; outlet names/states chosen for the test |
| `apc/login_banner.txt` | SSH login banner of a Schneider/APC rack PDU, captured in openbmc-test-automation `lib/pdu/schneider.robot`: https://github.com/openbmc/openbmc-test-automation | Yes |
| `apc/prodinfo.txt` | `prodInfo` output observed on an AP7920B, documented in AVI-SPL's APC PDU driver (`ProductInformation.java`): https://github.com/AVISPL/dal-avdevices-power-apc-pdu | Yes. Whether an `E000: Success` line comes first is unconfirmed; the parser accepts both |
| `apc/olon_success.txt` | APC NMC CLI result for `olOn`/`olOff` | Yes |
| `apc/e102_parameter_error.txt` | APC NMC CLI error code list (`E102: Parameter Error`) | Yes |
| `wti/powerplug_get_plug1.json` | WTI RESTful API introduction: https://wti.com/blogs/knowledge-base/restful-api-introduction (`GET /api/v2/config/powerplug`) | Yes |
| `wti/powerplug_post_plug1_on.json` | Same shape as the GET reply. The WTI docs don't show the POST reply body | No (assumed) |
| `wti/status_status.json` | Field samples from WTI's `cpm_status_info` Ansible module (`GET /api/v2/status/status`): https://github.com/wtinetworkgear/wti-collection | Fields yes, subset only |
| `raritan/pdu_getMetaData.json` | Xerus JSON-RPC curl example: https://help.servertech.com/json-rpc/4.3.10/md_curl-json-rpc.html | Yes |
| `raritan/outlet_setPowerState.json` | Same page (`setPowerState` on `/model/pdu/0/outlet/0`) | Yes |
| `raritan/outlet_getState_on.json` | Built from the `pdumodel.Outlet.State` IDL: https://help.raritan.com/json-rpc/pdu/v3.5.0/structpdumodel_1_1Outlet__2__1__4_1_1State.html | No (schema-derived; `ledState` omitted) |
| `raritan/pdu_getOutlets_8.json` | Built from the `pdumodel.Pdu.getOutlets` IDL (list of object references `{rid, type}`) | No (schema-derived) |
| `raritan/jsonrpc_error.json` | JSON-RPC 2.0 standard error object | No (standard, not vendor-sourced) |
