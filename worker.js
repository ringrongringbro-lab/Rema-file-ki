/**
 * worker.js — Cloudflare Worker controller (webhook + D1)
 * =========================================================
 * Kaam: Telegram commands sunna, settings / allowed users / queue sambhalna,
 * aur GitHub Actions ko workflow_dispatch bhejna. Download/extract/upload
 * ka saara heavy kaam GitHub worker (leech_worker.py) karta hai.
 *
 * Bindings / variables (Cloudflare dashboard):
 *   D1 binding : DB
 *   Secrets    : BOT_TOKEN, GITHUB_TOKEN, CALLBACK_SECRET, WEBHOOK_SECRET
 *   Variables  : OWNER_ID, REPO_NAME, WORKFLOW_FILE (leech.yml), BRANCH (main), MAX_ACTIVE (optional, default 3)
 */

const now = () => Math.floor(Date.now() / 1000);

/* ---------------- Telegram helpers ---------------- */
async function tg(env, method, params = {}) {
  try {
    const r = await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/${method}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
    });
    return await r.json();
  } catch (e) {
    console.log(`Telegram API error (${method}):`, String(e));
    return {};
  }
}

async function send(env, chat_id, text, extra = {}) {
  const r = await tg(env, "sendMessage", { chat_id, text, ...extra });
  return r.result && r.result.message_id;
}

const edit = (env, chat_id, message_id, text, extra = {}) =>
  tg(env, "editMessageText", { chat_id, message_id, text, ...extra });

/* ---------------- D1 helpers ---------------- */
const isOwner = (env, id) => String(id) === String(env.OWNER_ID);

async function isAllowed(env, id) {
  if (isOwner(env, id)) return true;
  const r = await env.DB.prepare("SELECT 1 AS ok FROM users WHERE user_id=?").bind(id).first();
  return !!r;
}

async function getSettings(env, id) {
  const r = await env.DB.prepare("SELECT thumb, format FROM settings WHERE user_id=?").bind(id).first();
  return { thumb: (r && r.thumb) || "", format: (r && r.format) || "media" };
}

async function setSetting(env, id, col, val) {
  if (!["thumb", "format"].includes(col)) return;
  await env.DB.prepare(
    `INSERT INTO settings (user_id, ${col}) VALUES (?, ?)
     ON CONFLICT(user_id) DO UPDATE SET ${col}=excluded.${col}`
  ).bind(id, val).run();
}

async function setPending(env, uid, kind, data, chat_id, ttl) {
  await env.DB.prepare(
    "INSERT OR REPLACE INTO pending (user_id, kind, data, chat_id, expires_at) VALUES (?,?,?,?,?)"
  ).bind(uid, kind, data || "", chat_id, now() + ttl).run();
}

const clearPending = (env, uid) =>
  env.DB.prepare("DELETE FROM pending WHERE user_id=?").bind(uid).run();

async function getPending(env, uid) {
  const p = await env.DB.prepare(
    "SELECT kind, data, chat_id, expires_at FROM pending WHERE user_id=?"
  ).bind(uid).first();
  if (!p) return null;
  if (p.expires_at < now()) {
    await clearPending(env, uid);
    return { ...p, expired: true };
  }
  return p;
}

function safeName(s) {
  return (s || "").replace(/[\\/:*?"<>|\r\n\t]/g, "").trim().replace(/^\.+|\.+$/g, "").slice(0, 150);
}

/* ---------------- GitHub ---------------- */
function ghHeaders(env) {
  return {
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "leech-controller",
    "Content-Type": "application/json",
  };
}

async function dispatch(env, t, st, triggerMsgId) {
  const url = `https://api.github.com/repos/${env.REPO_NAME}/actions/workflows/${env.WORKFLOW_FILE}/dispatches`;
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: ghHeaders(env),
      body: JSON.stringify({
        ref: env.BRANCH || "main",
        inputs: {
          url: t.url,
          chat_id: String(t.chat_id),
          trigger_msg_id: String(triggerMsgId || "none"),
          task_id: String(t.id),
          rename: t.rename || "",
          fmt: st.format,
          thumb: st.thumb || "",
          extract_subs: t.extract ? "true" : "false",
        },
      }),
    });
    if (r.status === 204) return [true, "Dispatched"];
    return [false, `Code ${r.status}: ${await r.text()}`];
  } catch (e) {
    return [false, String(e)];
  }
}

async function findRunId(env, taskId) {
  const re = new RegExp(`Leech task #${taskId}(?!\\d)`);
  for (const status of ["in_progress", "queued"]) {
    try {
      const r = await fetch(
        `https://api.github.com/repos/${env.REPO_NAME}/actions/workflows/${env.WORKFLOW_FILE}/runs?status=${status}&per_page=30`,
        { headers: ghHeaders(env) }
      );
      const j = await r.json();
      const hit = (j.workflow_runs || []).find((x) => re.test(x.display_title || x.name || ""));
      if (hit) return hit.id;
    } catch (e) { /* ignore */ }
  }
  return null;
}

/* ---------------- Queue ---------------- */
async function pump(env) {
  const max = parseInt(env.MAX_ACTIVE || "3", 10);
  const stale = now() - 6 * 3600; // 6 ghante se purana running task slot nahi gherega
  for (let i = 0; i < max; i++) {
    const t = await env.DB.prepare("SELECT * FROM tasks WHERE status='queued' ORDER BY id LIMIT 1").first();
    if (!t) return;
    const r = await env.DB.prepare(
      `UPDATE tasks SET status='running', started_at=?
       WHERE id=? AND status='queued'
       AND (SELECT COUNT(*) FROM tasks WHERE status='running' AND started_at>?) < ?`
    ).bind(now(), t.id, stale, max).run();
    if (!r.meta || !r.meta.changes) return; // slot khaali nahi

    const st = await getSettings(env, t.user_id);
    const mid = await send(env, t.chat_id, "⚡ Task Dispatched to GitHub Actions...");
    const [ok, info] = await dispatch(env, t, st, mid);
    if (!ok) {
      await env.DB.prepare("UPDATE tasks SET status='failed' WHERE id=?").bind(t.id).run();
      await send(env, t.chat_id, `❌ Dispatch Failed:\n\`${info}\``);
    }
  }
}

async function createTask(env, uid, chat, url, rename, extract) {
  const r = await env.DB.prepare(
    "INSERT INTO tasks (user_id, chat_id, url, rename, extract, status, created_at) VALUES (?,?,?,?,?, 'queued', ?)"
  ).bind(uid, chat, url, rename || "", extract ? 1 : 0, now()).run();
  const id = r.meta.last_row_id;
  await pump(env);
  const t = await env.DB.prepare("SELECT status FROM tasks WHERE id=?").bind(id).first();
  if (t && t.status === "queued") {
    await send(env, chat, `⏳ Task #${id} queue mein hai. Slot khaali hote hi start ho jayega.`);
  }
}

async function cancelTask(env, taskId, uid) {
  const t = await env.DB.prepare("SELECT * FROM tasks WHERE id=?").bind(taskId).first();
  if (!t) return "Task nahi mila.";
  if (!isOwner(env, uid) && t.user_id !== uid) return "Yeh task tumhara nahi hai.";
  if (!["queued", "running"].includes(t.status)) return `Task #${taskId} pehle se ${t.status} hai.`;

  await env.DB.prepare("UPDATE tasks SET status='cancelled' WHERE id=?").bind(taskId).run();
  if (t.status === "running") {
    const runId = t.run_id || (await findRunId(env, taskId));
    if (runId) {
      try {
        await fetch(`https://api.github.com/repos/${env.REPO_NAME}/actions/runs/${runId}/cancel`, {
          method: "POST",
          headers: ghHeaders(env),
        });
      } catch (e) { /* ignore */ }
    }
  }
  await pump(env);
  return `🛑 Task #${taskId} cancelled.`;
}

/* ---------------- Menus ---------------- */
const settingsKeyboard = (env, uid) => {
  const rows = [
    [{ text: "🖼 Thumbnail", callback_data: "set:thumb" }],
    [{ text: "📤 Format", callback_data: "set:format" }],
  ];
  if (isOwner(env, uid)) rows.push([{ text: "👥 Allowed Users", callback_data: "set:users" }]);
  return { inline_keyboard: rows };
};

async function sendCancelList(env, chat, uid) {
  const q = isOwner(env, uid)
    ? env.DB.prepare("SELECT id, status, url FROM tasks WHERE status IN ('queued','running') ORDER BY id LIMIT 30")
    : env.DB.prepare("SELECT id, status, url FROM tasks WHERE user_id=? AND status IN ('queued','running') ORDER BY id LIMIT 30").bind(uid);
  const { results } = await q.all();
  if (!results || !results.length) {
    await send(env, chat, "Koi active task nahi hai.");
    return;
  }
  const rows = results.map((t) => {
    let label = t.url;
    try { label = new URL(t.url).hostname; } catch (e) { label = t.url.slice(0, 20); }
    return [{ text: `🛑 #${t.id} ${t.status} — ${label}`.slice(0, 60), callback_data: `cx:${t.id}` }];
  });
  await send(env, chat, "Kaunsa task cancel karna hai?", { reply_markup: { inline_keyboard: rows } });
}

/* ---------------- Message handler ---------------- */
async function onMessage(env, msg) {
  const uid = msg.from && msg.from.id;
  const chat = msg.chat.id;
  if (!uid) return;

  const text = (msg.text || "").trim();
  const isCmd = text.startsWith("/");

  if (!(await isAllowed(env, uid))) {
    if (isCmd) await send(env, chat, "⛔ Access nahi hai.");
    return;
  }

  if (!isCmd) {
    await handlePendingInput(env, msg, uid, chat, text);
    return;
  }

  const cmd = text.split(/[\s@]/)[0].toLowerCase();
  const arg = text.replace(/^\/\S+\s*/, "").trim();
  await clearPending(env, uid); // koi bhi command purana wait cancel kar deta hai

  if (cmd === "/start") {
    await send(env, chat, "🙋‍♂️ Bot ready hai! /leech link bhejo. Subtitles alag chahiye to /leechextract link.");
  } else if (cmd === "/leech" || cmd === "/leechextract") {
    const extract = cmd === "/leechextract";
    const link = arg.split(/\s+/)[0] || "";
    if (!/^(https?:\/\/|magnet:\?)/i.test(link)) {
      await send(
        env, chat,
        `Usage:\n${cmd} link\n\nExample:\n${cmd} https://pixeldrain.com/u/xxxx\n${cmd} https://nyaa.si/download/2170047.torrent`
      );
      return;
    }
    await setPending(env, uid, "rename", JSON.stringify({ url: link, extract }), chat, 600);
    await send(env, chat, "✏️ You want rename this file?\nAgar haa toh name bhejo.\nAgar nahi toh skip ke liye S bhejo.");
  } else if (cmd === "/setting") {
    await send(env, chat, "⚙️ Settings", { reply_markup: settingsKeyboard(env, uid) });
  } else if (cmd === "/cancel") {
    if (/^\d+$/.test(arg)) {
      await send(env, chat, await cancelTask(env, parseInt(arg, 10), uid));
    } else {
      await sendCancelList(env, chat, uid);
    }
  }
}

async function handlePendingInput(env, msg, uid, chat, text) {
  const p = await getPending(env, uid);
  if (!p) return;

  if (p.expired) {
    if (p.kind === "thumb") await send(env, chat, "⏰ Time over. /setting se Thumbnail dobara dabao.");
    else if (p.kind === "rename") await send(env, chat, "⏰ Time over. /leech dobara bhejo.");
    return;
  }

  if (p.kind === "thumb") {
    let fid = null;
    if (msg.photo && msg.photo.length) fid = msg.photo[msg.photo.length - 1].file_id;
    else if (msg.document && (msg.document.mime_type || "").startsWith("image/")) fid = msg.document.file_id;
    if (!fid) {
      await send(env, chat, "Image bhejo (photo ya image file).");
      return;
    }
    await setSetting(env, uid, "thumb", fid);
    await clearPending(env, uid);
    await send(env, chat, "✅ Thumbnail save ho gaya. Ab har leech isi thumbnail ke saath aayegi.");
    return;
  }

  if (p.kind === "rename") {
    if (!text) return;
    const { url, extract } = JSON.parse(p.data);
    await clearPending(env, uid);
    const skip = text.toUpperCase() === "S";
    const rename = skip ? "" : safeName(text);
    if (!skip && !rename) {
      await send(env, chat, "Naam sahi nahi hai, /leech dobara bhejo.");
      return;
    }
    await createTask(env, uid, chat, url, rename, !!extract);
    return;
  }

  if (p.kind === "add_user") {
    if (!/^\d{5,15}$/.test(text)) {
      await send(env, chat, "Sirf number wali Telegram user ID bhejo.");
      return;
    }
    if (isOwner(env, text)) {
      await clearPending(env, uid);
      await send(env, chat, "Owner pehle se allowed hai.");
      return;
    }
    await env.DB.prepare("INSERT OR IGNORE INTO users (user_id, added_at) VALUES (?, ?)")
      .bind(parseInt(text, 10), now()).run();
    await clearPending(env, uid);
    await send(env, chat, `✅ User ${text} add ho gaya.`);
    return;
  }

  if (p.kind === "del_confirm") {
    await clearPending(env, uid);
    if (text.toLowerCase() === "yes") {
      const id = parseInt(p.data, 10);
      await env.DB.prepare("DELETE FROM users WHERE user_id=?").bind(id).run();
      await env.DB.prepare("DELETE FROM settings WHERE user_id=?").bind(id).run();
      await send(env, chat, `🗑 User ${id} delete ho gaya.`);
    } else {
      await send(env, chat, "Cancelled. User delete nahi hua.");
    }
  }
}

/* ---------------- Callback (button) handler ---------------- */
async function onCallback(env, cq) {
  const uid = cq.from.id;
  const chat = cq.message && cq.message.chat.id;
  const mid = cq.message && cq.message.message_id;
  await tg(env, "answerCallbackQuery", { callback_query_id: cq.id });
  if (!chat || !(await isAllowed(env, uid))) return;

  const [a, b] = (cq.data || "").split(":");

  if (a === "set" && b === "thumb") {
    const st = await getSettings(env, uid);
    await setPending(env, uid, "thumb", "", chat, 50);
    await send(
      env, chat,
      `🖼 50 second ke andar thumbnail image bhejo (koi bhi image format chalega).${st.thumb ? "\n(Abhi ek thumbnail set hai, naya bhejoge to replace ho jayega.)" : ""}`,
      { reply_markup: { inline_keyboard: [[{ text: "🗑 Remove current thumbnail", callback_data: "thumb:remove" }]] } }
    );
  } else if (a === "thumb" && b === "remove") {
    await setSetting(env, uid, "thumb", "");
    await clearPending(env, uid);
    await edit(env, chat, mid, "🗑 Thumbnail hata diya.");
  } else if (a === "set" && b === "format") {
    const st = await getSettings(env, uid);
    await send(env, chat, "📤 Upload format chuno:", {
      reply_markup: {
        inline_keyboard: [[
          { text: (st.format === "media" ? "✅ " : "") + "Media", callback_data: "fmt:media" },
          { text: (st.format === "document" ? "✅ " : "") + "Document", callback_data: "fmt:document" },
        ]],
      },
    });
  } else if (a === "fmt" && (b === "media" || b === "document")) {
    await setSetting(env, uid, "format", b);
    await edit(env, chat, mid, `✅ Format set: ${b === "media" ? "Media" : "Document"}`);
  } else if (a === "set" && b === "users") {
    if (!isOwner(env, uid)) return;
    await send(env, chat, "👥 Allowed Users", {
      reply_markup: {
        inline_keyboard: [[
          { text: "➕ Add New User", callback_data: "usr:add" },
          { text: "➖ Delete User", callback_data: "usr:del" },
        ]],
      },
    });
  } else if (a === "usr" && b === "add") {
    if (!isOwner(env, uid)) return;
    await setPending(env, uid, "add_user", "", chat, 300);
    await send(env, chat, "Add karne wale user ki Telegram user ID bhejo.");
  } else if (a === "usr" && b === "del") {
    if (!isOwner(env, uid)) return;
    const { results } = await env.DB.prepare("SELECT user_id FROM users ORDER BY added_at LIMIT 100").all();
    if (!results || !results.length) {
      await send(env, chat, "Koi allowed user nahi hai.");
      return;
    }
    const rows = results.map((u) => [{ text: String(u.user_id), callback_data: `udel:${u.user_id}` }]);
    await send(env, chat, "Kaunsa user delete karna hai?", { reply_markup: { inline_keyboard: rows } });
  } else if (a === "udel") {
    if (!isOwner(env, uid) || !/^\d+$/.test(b || "")) return;
    await setPending(env, uid, "del_confirm", b, chat, 120);
    await send(env, chat, `ID ${b} delete karna hai?\nConfirm ke liye yes type karo.`);
  } else if (a === "cx" && /^\d+$/.test(b || "")) {
    await send(env, chat, await cancelTask(env, parseInt(b, 10), uid));
  }
}

/* ---------------- GitHub -> Worker callback ---------------- */
async function onGithubCallback(request, env) {
  if (!env.CALLBACK_SECRET || request.headers.get("X-Callback-Secret") !== env.CALLBACK_SECRET) {
    return new Response("forbidden", { status: 403 });
  }
  const b = await request.json();
  const id = parseInt(b.task_id, 10);
  if (!id) return new Response("bad", { status: 400 });

  if (b.event === "start") {
    const t = await env.DB.prepare("SELECT status FROM tasks WHERE id=?").bind(id).first();
    if (!t || t.status === "cancelled") {
      return new Response(JSON.stringify({ cancelled: true }), { headers: { "content-type": "application/json" } });
    }
    if (b.run_id) await env.DB.prepare("UPDATE tasks SET run_id=? WHERE id=?").bind(parseInt(b.run_id, 10), id).run();
    return new Response(JSON.stringify({ cancelled: false }), { headers: { "content-type": "application/json" } });
  }

  if (b.event === "done") {
    const final = b.status === "success" ? "done" : b.status === "cancelled" ? "cancelled" : "failed";
    await env.DB.prepare(
      "UPDATE tasks SET status = CASE WHEN status='cancelled' THEN 'cancelled' ELSE ? END WHERE id=?"
    ).bind(final, id).run();
    await pump(env);
    return new Response("ok");
  }
  return new Response("ok");
}

/* ---------------- One-time setup (webhook + commands) ---------------- */
async function setup(env, url) {
  if (!env.WEBHOOK_SECRET || url.searchParams.get("key") !== env.WEBHOOK_SECRET) {
    return new Response("forbidden", { status: 403 });
  }
  const webhook = await tg(env, "setWebhook", {
    url: `${url.origin}/`,
    secret_token: env.WEBHOOK_SECRET,
    allowed_updates: ["message", "callback_query"],
    drop_pending_updates: true,
  });
  const commands = await tg(env, "setMyCommands", {
    commands: [
      { command: "leech", description: "Link se file leech karo" },
      { command: "leechextract", description: "Leech + video ke subtitles alag bhejo" },
      { command: "setting", description: "Thumbnail, Format, Allowed Users" },
      { command: "cancel", description: "Task cancel karo" },
    ],
  });
  return new Response(JSON.stringify({ webhook, commands }, null, 2), {
    headers: { "content-type": "application/json" },
  });
}

/* ---------------- Entry ---------------- */
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      if (url.pathname === "/setup") return await setup(env, url);
      if (url.pathname === "/cb" && request.method === "POST") return await onGithubCallback(request, env);

      if (request.method === "POST") {
        if (
          env.WEBHOOK_SECRET &&
          request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET
        ) {
          return new Response("forbidden", { status: 403 });
        }
        const update = await request.json();
        if (update.message) await onMessage(env, update.message);
        else if (update.callback_query) await onCallback(env, update.callback_query);
        return new Response("ok");
      }
      return new Response("Leech controller running ✅");
    } catch (e) {
      console.log("ERR", (e && e.stack) || String(e));
      return new Response("ok"); // Telegram retry storm se bachne ke liye hamesha 200
    }
  },
};
