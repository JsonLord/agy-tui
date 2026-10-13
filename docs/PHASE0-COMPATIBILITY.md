# Phase 0 inventory and deferred live gates

Read the complete seed spec and the preparation report on `hybrid-optimised`
(e5b416d70b4dd837514c16c710c598fd62eb7b7c). Its claims are historical input,
not evidence of this cloud instance or official client compatibility.

Actual legacy `mcp_gateway.py` starts `MCPServer('antigravity-hybrid')`, loads
root `.env`, uses Settings planner/worker model endpoints and native wrapper
helpers. Legacy `app/config.py` retains fixed provider/model defaults and native
credential-config parsing. Legacy sentinel adapter is a process-wait seam,
not Microsoft Argus's Manager/Planner/Engineer/Reviewer runtime. Containers,
Debian services and host-specific recovery/tests remain legacy material.

The new isolated `app/hybrid_local` package avoids those legacy providers and
imports only pure `app/schemas.py` for compatibility wrappers. Its launcher is
absolute-path stdio with its own registry/ledger. Owned permission-scoped ACP
sessions inherit native OpenCode model/provider configuration without `--model`,
model-setting RPC, URL injection or duplicated credentials. External native
plugins are disabled with --pure; plugin-dependent workflows need later review.

Official AGY cloud/client tool access and native `/goal` dispatch are unverified.
Treat `/goal` only as an instruction convention until actual device evidence
supports native dispatch. No new AGY UI or Argus role engine is implemented.
Maintained `JsonLord/microsoft-argus` runtime reuse remains required later;
no Argus dependency/runtime or ClickUp production access is installed here.
Per user, these gates and authenticated work-PC model/provider checks are deferred
to Phase 1.5, after installation. No public endpoints or original Debian changes.
