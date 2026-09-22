# TigerGraph MCP transcript — HHG-014

Total wall-clock: **21.98s** · agent tool_calls counter: **11** · MCP call records captured: **11**.

Final state: verdict = **fraud**, p_initial = 0.403, p_final = 0.859, pattern = **undocumented**, case vertex = `CASE-HHG-014`.

Ledger channels: `['customer', 'history', 'identity_flags']`.


## Per-tool call log

| # | tool | params (summarised) | latency | response head |
|---|---|---|---|---|
| 1 | `tigergraph__run_installed_query` | `{'graph_name': 'FraudGraph', 'query_name': 'burst_48h', 'params': {'p_card': {'id': 'C13487\|555.0\|150.0\|mastercard\|117.0` | 0.71s | ````json {   "success": true,   "operation": "run_installed_query",   "data": {     "query_name": "bu` |
| 2 | `tigergraph__run_installed_query` | `{'graph_name': 'FraudGraph', 'query_name': 'testing_sequence', 'params': {'p_card': {'id': 'C13487\|555.0\|150.0\|mastercar` | 0.71s | ````json {   "success": true,   "operation": "run_installed_query",   "data": {     "query_name": "te` |
| 3 | `tigergraph__run_installed_query` | `{'graph_name': 'FraudGraph', 'query_name': 'recurring_match', 'params': {'p_card': {'id': 'C13487\|555.0\|150.0\|mastercard` | 0.71s | ````json {   "success": true,   "operation": "run_installed_query",   "data": {     "query_name": "re` |
| 4 | `tigergraph__run_installed_query` | `{'graph_name': 'FraudGraph', 'query_name': 'region_history', 'params': {'p_card': {'id': 'C13487\|555.0\|150.0\|mastercard\|` | 0.75s | ````json {   "success": true,   "operation": "run_installed_query",   "data": {     "query_name": "re` |
| 5 | `tigergraph__run_installed_query` | `{'graph_name': 'FraudGraph', 'query_name': 'ring_components', 'params': {'p_card': {'id': 'C13487\|555.0\|150.0\|mastercard` | 0.75s | ````json {   "success": true,   "operation": "run_installed_query",   "data": {     "query_name": "ri` |
| 6 | `tigergraph__run_installed_query` | `{'graph_name': 'FraudGraph', 'query_name': 'near_threshold_burst', 'params': {'p_card': {'id': 'C13487\|555.0\|150.0\|maste` | 0.76s | ````json {   "success": true,   "operation": "run_installed_query",   "data": {     "query_name": "ne` |
| 7 | `tigergraph__run_installed_query` | `{'graph_name': 'FraudGraph', 'query_name': 'region_cluster', 'params': {'p_region': {'id': '191.0'}, 'p_window_start': '` | 0.76s | ````json {   "success": true,   "operation": "run_installed_query",   "data": {     "query_name": "re` |
| 8 | `tigergraph__run_installed_query` | `{'graph_name': 'FraudGraph', 'query_name': 'device_neighbors', 'params': {'p_device': {'id': 'SM-G935F Build/NRD90M \| An` | 0.82s | ````json {   "success": true,   "operation": "run_installed_query",   "data": {     "query_name": "de` |
| 9 | `tigergraph__run_installed_query` | `{'graph_name': 'FraudGraph', 'query_name': 'closed_cases_touching', 'params': {'p_card': {'id': 'C13487\|555.0\|150.0\|mast` | 2.25s | ````json {   "success": true,   "operation": "run_installed_query",   "data": {     "query_name": "cl` |
| 10 | `tigergraph__run_installed_query` | `{'graph_name': 'FraudGraph', 'query_name': 'vector_search_cc', 'params': {'p_query_vector': [-0.07281777262687683, -0.00` | 5.94s | ````json {   "success": true,   "operation": "run_installed_query",   "data": {     "query_name": "ve` |
| 11 | `tigergraph__run_installed_query` | `{'graph_name': 'FraudGraph', 'query_name': 'write_case', 'params': {'p_case_id': 'CASE-HHG-014', 'p_hhg_id': 'HHG-014', ` | 0.72s | ````json {   "success": true,   "operation": "run_installed_query",   "data": {     "query_name": "wr` |

## Actions from the answer

- **BLOCK_CARD** (`L1`) — R2: customer denied — block + reissue
- **CREATE_CASE** (`auto`) — R2: customer denied
- **FILE_REPORT** (`L2`) — R2 + §3a: exposure>$1,000 or shared element/undocumented
- **MONITOR_CONNECTED_CARDS** (`auto`) — R2/R6: monitor cards sharing this device