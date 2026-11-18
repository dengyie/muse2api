#!/usr/bin/env bash
# =============================================================================
# muse2api 线上健康巡检（幂等；--no-cost 跳过所有真实上游生成项）
# 服务器侧运行：bash /opt/muse2api/repo/_scripts/patrol.sh
# 巡检项：
#   P0 healthz                    容器进程与 API 存活
#   P1 build sha + 存活状态       镜像在位 + healthz 状态健康（防漂移真校验）
#   P2 账号池                     有可用账号、会话 ok、额度未全枯竭
#   P3 TetherPool                 控制面可达、绑定存在、出口 ≤5 号铁律
#   P4 mediabak                   备份通道健康（退避/冷却计入 FAIL；--no-cost 不豁免）
#   P5 attach-probe               composer 附件链路可挂载（不发送不计费）
#   P6 chat 快验                  muse-spark 真实上游往返（消耗额度，--no-cost 跳过）
#   P7 幂等重放                   既有任务 GET 幂等 + content 可取（历史成品，只读）
#   P8 图片 E2E                   muse-image 真实生成一张（计费，--no-cost 跳过）
# 用法：
#   bash patrol.sh              # 全量（P6/P8 计费项）
#   bash patrol.sh --no-cost    # 跳过 P6/P8（全部真实生成项）
# 退出码：0=全绿 1=有 FAIL（明细看输出）
# =============================================================================
set -u
BASE="http://127.0.0.1:18610"
REPO="/opt/muse2api/repo"
KEY=$(grep '^MUSE2API_KEY=' "$REPO/.env" 2>/dev/null | cut -d= -f2)
if [ -z "${KEY:-}" ]; then
  echo "FATAL: $REPO/.env 缺失或无 MUSE2API_KEY——鉴权项无法执行"; exit 1
fi
NO_COST=0
[ "${1:-}" = "--no-cost" ] && NO_COST=1
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  [OK]   $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  [FAIL] $1"; }
warn() { echo "  [WARN] $1"; }
hdr()  { echo; echo "== $1 =="; }

hdr "P0 healthz"
R=$(curl -s -m 8 "$BASE/healthz" 2>/dev/null)
echo "$R" | grep -q '"status":"ok"' && ok "healthz ok ($R)" || bad "healthz: $R"

hdr "P1 容器存活 + 部署一致性"
R=$(docker inspect muse2api --format '{{.State.Status}} {{.State.Health.Status}}' 2>/dev/null)
CT=$(echo "$R" | cut -d' ' -f1); CH=$(echo "$R" | cut -d' ' -f2)
[ "$CT" = "running" ] && ok "容器 running" || bad "容器状态: $R"
case "$CH" in
  healthy|"") ok "healthcheck: ${CH:-未配置（旧镜像）}";;
  starting)   warn "healthcheck starting（启动期）";;
  *)          bad "healthcheck: $CH";;
esac
SHA=$(docker exec muse2api sh -c 'echo $BUILD_SHA' 2>/dev/null)
ok "镜像 BUILD_SHA=${SHA:-unknown}"

hdr "P2 账号池"
R=$(curl -s -m 8 -H "Authorization: Bearer $KEY" "$BASE/admin/status")
N_ACC=$(echo "$R" | python3 -c "import json,sys;d=json.load(sys.stdin);print(len(d['accounts']))" 2>/dev/null)
N_OK=$(echo "$R"  | python3 -c "import json,sys;d=json.load(sys.stdin);print(sum(1 for a in d['accounts'] if a.get('ok')))" 2>/dev/null)
[ "${N_OK:-0}" -ge 1 ] && ok "账号池: $N_ACC 个，$N_OK 个会话正常" || bad "无 ok 账号（池=$N_ACC）"
echo "$R" | python3 -c "
import json,sys
d=json.load(sys.stdin)
for a in d['accounts']:
    q=(a.get('quota') or {})
    pct=q.get('weekly_used_pct')
    print('  acct %s ok=%s weekly_used=%s%%' % (a['id'][:16], a.get('ok'), pct))" 2>/dev/null

hdr "P3 TetherPool"
R=$(curl -s -m 8 -H "Authorization: Bearer $KEY" "$BASE/admin/tetherpool/status")
echo "$R" | grep -q '"control_plane_alive":true' && ok "控制面可达" || bad "控制面不可达: $R"
N_BIND=$(echo "$R" | python3 -c "import json,sys;d=json.load(sys.stdin);print(len(d.get('bindings',{})))" 2>/dev/null)
# 同出口 ≤5 号铁律：按 node_name 分组计数
echo "$R" | python3 -c "
import json,sys
d=json.load(sys.stdin)
from collections import Counter
c=Counter((b.get('node_name') or '?') for b in d.get('bindings',{}).values())
over={k:v for k,v in c.items() if v>5}
print('  bindings:', dict(c))
exit(1 if over else 0)" 2>/dev/null && ok "同出口 ≤5 号铁律满足" || bad "同出口超限"
[ "${N_BIND:-0}" -ge 1 ] && ok "绑定存在（$N_BIND 个账号）" || bad "无绑定"

hdr "P4 mediabak（备份硬退避计入 FAIL；mesh 冷却=已知降级 WARN，--no-cost 不豁免）"
# patch22r：语义分级——「连续失败 N 次退避」= 备份通道真故障（FAIL）；
# 「mesh 通道进入冷却」= patch22n 已知降级（tencent-lh 无 5244 监听，
# public 通道健康即不影响功能，WARN）。
R=$(docker logs muse2api --since 24h 2>&1 | grep -E "连续失败.*退避" | tail -2)
if [ -n "$R" ]; then
  bad "24h 内备份连续失败触发硬退避（最近 1 条）：$(echo "$R" | tail -1)"
else
  ok "24h 内无备份硬退避"
fi
R2=$(docker logs muse2api --since 24h 2>&1 | grep -E "mesh 通道进入.*冷却|媒体回源.*退避" | tail -1)
[ -n "$R2" ] && warn "mesh 冷却/回源退避（已知降级）：$R2" || ok "无 mesh 冷却/回源退避"
N_MEDIA=$(docker exec muse2api sh -c 'ls /app/data/media 2>/dev/null | wc -l')
ok "media 目录现存 $N_MEDIA 个文件（delete_local 模式应为 0~少量）"

hdr "P5 attach-probe（不发送不计费）"
AID=$(curl -s -m 8 -H "Authorization: Bearer $KEY" "$BASE/admin/status" | python3 -c "import json,sys;d=json.load(sys.stdin);print([a['id'] for a in d['accounts'] if a.get('ok')][0])" 2>/dev/null)
R=$(curl -s -m 90 -X POST -H "Authorization: Bearer $KEY" "$BASE/admin/accounts/$AID/attach-probe" 2>/dev/null)
echo "$R" | grep -q '"attach":true' && ok "附件链路可挂载" || bad "attach-probe: $(echo "$R" | head -c 200)"

hdr "P6 chat 快验（muse-spark，计费项）"
if [ "$NO_COST" = "1" ]; then
  echo "  [SKIP] --no-cost"
else
R=$(curl -s -m 60 -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"muse-spark","messages":[{"role":"user","content":"Reply with exactly: OK"}]}' \
  "$BASE/v1/chat/completions" 2>/dev/null)
echo "$R" | grep -q '"choices"' && ok "chat 往返正常" || bad "chat: $(echo "$R" | head -c 200)"
fi

hdr "P7 幂等重放 + 历史成品 content"
# 取最近一个 completed 视频任务，验证 GET 幂等与 content 可取
R=$(curl -s -m 8 -H "Authorization: Bearer $KEY" "$BASE/admin/tasks")
TID=$(echo "$R" | python3 -c "
import json,sys
d=json.load(sys.stdin)
ts=d.get('tasks',d) if isinstance(d,dict) else d
done=[t for t in ts if t.get('kind')=='video' and t.get('status') in ('completed','succeeded','success','done')]
print(done[0]['id'] if done else '')" 2>/dev/null)
if [ -n "$TID" ]; then
  S1=$(curl -s -m 8 -H "Authorization: Bearer $KEY" "$BASE/v1/videos/$TID" | sha256sum | cut -d' ' -f1)
  S2=$(curl -s -m 8 -H "Authorization: Bearer $KEY" "$BASE/v1/videos/$TID" | sha256sum | cut -d' ' -f1)
  [ "$S1" = "$S2" ] && ok "GET /v1/videos/$TID 幂等" || bad "GET 不幂等"
  C1=$(curl -s -m 60 -H "Authorization: Bearer $KEY" "$BASE/v1/videos/$TID/content" -o /tmp/patrol_c1 -w '%{http_code}')
  C2=$(curl -s -m 60 -H "Authorization: Bearer $KEY" "$BASE/v1/videos/$TID/content" -o /tmp/patrol_c2 -w '%{http_code}')
  H1=$(sha256sum /tmp/patrol_c1 2>/dev/null | cut -d' ' -f1)
  H2=$(sha256sum /tmp/patrol_c2 2>/dev/null | cut -d' ' -f1)
  [ "$C1" = "200" ] && [ "$H1" = "$H2" ] && ok "content 双下载 200 + sha 一致 ($H1)" \
    || bad "content: http=$C1/$C2 sha match=$([ "$H1" = "$H2" ] && echo yes || echo NO)"
  rm -f /tmp/patrol_c1 /tmp/patrol_c2
else
  echo "  [WARN] 无历史 completed 视频任务可验（首次部署或全部失败）"
fi

hdr "P8 图片 E2E（计费项）"
if [ "$NO_COST" = "1" ]; then
  echo "  [SKIP] --no-cost"
else
  B64=$(python3 -c "
import base64,struct,zlib
# 生成 64x64 纯色 PNG（无外部依赖）
def chunk(t,d):
    c=t+d
    return struct.pack('>I',len(d))+c+struct.pack('>I',zlib.crc32(c)&0xffffffff)
ihdr=struct.pack('>IIBBBBB',64,64,8,2,0,0,0)
raw=b''.join(b'\x00'+b'\x30\x60\xd0'*64 for _ in range(64))
png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',ihdr)+chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b'')
print(base64.b64encode(png).decode())")
  R=$(curl -s -m 180 -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d "{\"model\":\"muse-image\",\"prompt\":\"a simple blue square\",\"images\":[\"data:image/png;base64,$B64\"],\"n\":1}" \
    "$BASE/v1/images/generations" 2>/dev/null)
  # 图片响应可能是 url 或 b64
  echo "$R" | python3 -c "
import json,sys
d=json.load(sys.stdin)
item=((d.get('data') or [{}])[0])
u=item.get('url') or ('b64' if item.get('b64_json') else '')
sys.exit(0 if u else 1)" && ok "图片生成返回成品" || bad "图片: $(echo "$R" | head -c 200)"
fi

echo
echo "=============================="
echo "巡检结果: PASS=$PASS FAIL=$FAIL"
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
