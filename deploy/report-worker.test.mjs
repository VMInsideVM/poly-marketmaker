// deploy/report-worker.test.mjs — 周报中继 Worker 的零依赖单测(node:test,不需要额外依赖/构建)。
//
// stub 掉全局 fetch,构造 Request 调 worker.fetch(req, env),覆盖校验链、字段清洗与截断的回归。
// 跑法:node --test deploy/  (或 node --test deploy/report-worker.test.mjs)

import { test } from "node:test";
import assert from "node:assert/strict";
import worker from "./report-worker.js";

const KEY = "test-key";

function baseEnv(extra = {}) {
  return { CLIENT_KEY: KEY, TG_TOKEN: "tok", TG_CHAT_ID: "chat1", ...extra };
}

function basePayload(extra = {}) {
  return {
    v: 1,
    senders: [],
    week_start: "2026-07-20",
    week_end: "2026-07-26",
    daily_nets: [["2026-07-20", 1]],
    week_totals: {
      reward: 1,
      rebate: 0,
      sell_profit: 0,
      loss: 0,
      fee: 0,
      net: 1,
    },
    cumulative_net: 1,
    since_date: "2026-05-17",
    per_wallet: [],
    ...extra,
  };
}

// opts: { method, key, noKey, rawBody, body }。method 为 GET/HEAD 时不带 body(Fetch 规范不允许)。
function req(opts = {}) {
  const method = opts.method ?? "POST";
  const headers = {};
  if (!opts.noKey) headers["x-mm-key"] = opts.key ?? KEY;
  const init = { method, headers };
  if (method !== "GET" && method !== "HEAD") {
    headers["content-type"] = "application/json";
    init.body =
      opts.rawBody !== undefined
        ? opts.rawBody
        : JSON.stringify(opts.body ?? basePayload());
  }
  return new Request("https://worker.example/", init);
}

function stubFetch(ok = true) {
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    calls.push({ url, opts });
    return { ok };
  };
  return calls;
}

// ---- 1. ENABLED 判宽:"0"/"0 "/"false"/"OFF" 都得 503,且不转发 ----

for (const v of ["0", "0 ", "false", "OFF"]) {
  test(`ENABLED=${JSON.stringify(v)} -> 503,fetch 未被调用`, async () => {
    const calls = stubFetch();
    const res = await worker.fetch(req(), baseEnv({ ENABLED: v }));
    assert.equal(res.status, 503);
    assert.equal(calls.length, 0);
  });
}

// ---- 2. ENABLED 未设 / "1" -> 放行到后续检查 ----

test("ENABLED 未设 -> 放行", async () => {
  stubFetch();
  const res = await worker.fetch(req(), baseEnv());
  assert.equal(res.status, 200);
});

test('ENABLED="1" -> 放行', async () => {
  stubFetch();
  const res = await worker.fetch(req(), baseEnv({ ENABLED: "1" }));
  assert.equal(res.status, 200);
});

// ---- 3. 方法 / key 校验 ----

test("GET -> 405", async () => {
  stubFetch();
  const res = await worker.fetch(req({ method: "GET" }), baseEnv());
  assert.equal(res.status, 405);
});

test("X-MM-Key 错 -> 403", async () => {
  stubFetch();
  const res = await worker.fetch(req({ key: "wrong" }), baseEnv());
  assert.equal(res.status, 403);
});

test("缺 X-MM-Key -> 403", async () => {
  stubFetch();
  const res = await worker.fetch(req({ noKey: true }), baseEnv());
  assert.equal(res.status, 403);
});

// ---- 4. body 校验 ----

test("body 为 JSON null -> 400", async () => {
  stubFetch();
  const res = await worker.fetch(req({ rawBody: "null" }), baseEnv());
  assert.equal(res.status, 400);
});

test("body 不是 JSON -> 400", async () => {
  stubFetch();
  const res = await worker.fetch(req({ rawBody: "not json" }), baseEnv());
  assert.equal(res.status, 400);
});

// ---- 5. daily_nets/per_wallet 非数组不抛异常 ----

test("daily_nets=5, per_wallet=true -> 不抛异常,200", async () => {
  stubFetch();
  const res = await worker.fetch(
    req({ body: basePayload({ daily_nets: 5, per_wallet: true }) }),
    baseEnv()
  );
  assert.equal(res.status, 200);
});

// ---- 6. 日期字段格式 ----

test("week_start 不合日期格式 -> 400", async () => {
  stubFetch();
  const res = await worker.fetch(
    req({ body: basePayload({ week_start: "2026/07/20" }) }),
    baseEnv()
  );
  assert.equal(res.status, 400);
});

test("since_date 不合日期格式 -> 400", async () => {
  stubFetch();
  const res = await worker.fetch(
    req({ body: basePayload({ since_date: "20260517" }) }),
    baseEnv()
  );
  assert.equal(res.status, 400);
});

// ---- 7. ALLOW 白名单 ----

test("ALLOW 留空 -> 放行", async () => {
  stubFetch();
  const res = await worker.fetch(
    req({ body: basePayload({ senders: ["0xzzz"] }) }),
    baseEnv()
  );
  assert.equal(res.status, 200);
});

test("ALLOW 填了但 senders 不命中 -> 403", async () => {
  stubFetch();
  const res = await worker.fetch(
    req({ body: basePayload({ senders: ["0xbbb"] }) }),
    baseEnv({ ALLOW: "0xaaa" })
  );
  assert.equal(res.status, 403);
});

test("ALLOW 命中(大小写混合、带空格) -> 200", async () => {
  stubFetch();
  const res = await worker.fetch(
    req({ body: basePayload({ senders: ["0xbbb"] }) }),
    baseEnv({ ALLOW: " 0xAAA, 0xBBB " })
  );
  assert.equal(res.status, 200);
});

// ---- 8. label 清洗:控制字符/零宽/双向控制符不进转发文本 ----

test("label 清洗:U+2028/U+202E/U+0085/零宽字符不进转发文本", async () => {
  const calls = stubFetch();
  const dirty = "A\u2028B\u202EC\u0085D\u200bE";
  const res = await worker.fetch(
    req({ body: basePayload({ per_wallet: [{ label: dirty, net: 1 }] }) }),
    baseEnv()
  );
  assert.equal(res.status, 200);
  const sent = JSON.parse(calls[0].opts.body);
  assert.ok(!/[\u2028\u202e\u0085\u200b]/.test(sent.text));
  assert.ok(sent.text.includes("ABCDE"));
});

// ---- 9. label 截断:emoji 代理对不被劈开 ----

test("label 截断:19 字符 + emoji,text 里没有孤立代理", async () => {
  const calls = stubFetch();
  const smiling = "a".repeat(19) + "\u{1F642}";
  const res = await worker.fetch(
    req({ body: basePayload({ per_wallet: [{ label: smiling, net: 1 }] }) }),
    baseEnv()
  );
  assert.equal(res.status, 200);
  const sent = JSON.parse(calls[0].opts.body);
  assert.ok(sent.text.includes(smiling)); // 20 码点内,整个 emoji 应完整保留
  const highs = (sent.text.match(/[\ud800-\udbff]/g) || []).length;
  const lows = (sent.text.match(/[\udc00-\udfff]/g) || []).length;
  assert.equal(highs, lows); // 高低代理必须成对,不能有孤立的一半
});

// ---- 10. 转发 body 结构 ----

test("转发 body: chat_id 恒等于 env.TG_CHAT_ID、含 disable_web_page_preview,payload 塞 chat_id 不改目标", async () => {
  const calls = stubFetch();
  const res = await worker.fetch(
    req({ body: basePayload({ chat_id: "evil-chat" }) }),
    baseEnv({ TG_CHAT_ID: "real-chat" })
  );
  assert.equal(res.status, 200);
  const sent = JSON.parse(calls[0].opts.body);
  assert.equal(sent.chat_id, "real-chat");
  assert.equal(sent.disable_web_page_preview, true);
});
