// GLKVM 自签证书放行（11-09 用户在对话里确认：「允许，指纹相符就放行」；decide 2760）
// GLKVM 的证书是设备自签（CN=localhost，有效期 1970–1979），ego 对它的「继续访问」授权约 7 天过期，过期后 bridge 打不开页面。
// 做法：页面跳转报 ERR_CERT_* 时，先用 Node 直连 GLKVM 取证书 SHA-256 指纹，和登记文件 .glkvm_cert_pin 比对；
// 一致才在拦截页上放行，放行后再经 CDP 取浏览器实际接受的证书复核一次，不一致立刻离开页面。
// 指纹不一致（设备重置换了证书，或有人冒充）一律不放行：写 /tmp/kvmphone/cert_mismatch.json，日志打 GLKVM_CERT_CHANGED，
// 由用户在本机终端运行 kpcert 核对后重新登记。
const tls = await import("node:tls");
const crypto = await import("node:crypto");
const fs = await import("node:fs/promises");

const HOST = "glkvm.local";
const RUN = "/tmp/kvmphone";

export const certFingerprint = () => new Promise((ok) => {
  const s = tls.connect({ host: HOST, port: 443, rejectUnauthorized: false, timeout: 8000 }, () => {
    const fp = s.getPeerCertificate()?.fingerprint256 || ""; s.end(); ok(fp.toUpperCase());
  });
  s.on("error", () => ok(""));
  s.on("timeout", () => { s.destroy(); ok(""); });
});

const readPin = async (dir) => (await fs.readFile(`${dir}/.glkvm_cert_pin`, "utf8").catch(() => "")).trim().toUpperCase();

const derFp = (b64) => crypto.createHash("sha256").update(Buffer.from(b64, "base64")).digest("hex").toUpperCase().match(/../g).join(":");

// 浏览器实际接受的证书（CDP Network.getCertificate）；取不到返回 ""，不当成不一致
const browserFp = async (page) => {
  try {
    const r = await page.cdp("Network.getCertificate", { origin: `https://${HOST}` });
    const der = (r && (r.tableNames || r.result?.tableNames)) || [];
    return der.length ? derFp(der[0]) : "";
  } catch { return ""; }
};

const mismatch = async (why, seen, pin, log) => {
  const rec = { at: new Date().toISOString(), why, seen, pin };
  await fs.mkdir(RUN, { recursive: true }).catch(() => {});
  await fs.writeFile(`${RUN}/cert_mismatch.json`, JSON.stringify(rec, null, 1)).catch(() => {});
  log?.(`GLKVM_CERT_CHANGED ${why} seen=${seen || "无"}`);
  console.log(`GLKVM_CERT_CHANGED：GLKVM 证书和登记的不一致（${why}），不放行。确认是设备自己换的证书后，在本机终端运行 kpcert 重新登记。`);
  throw new Error("GLKVM_CERT_CHANGED");
};

// 代替 page.goto(GLKVM 地址)：证书授权还在就和原来一样；过期了才走核对放行
export async function gotoGLKVM(page, url, dir, log) {
  try { await page.goto(url); return; } catch (e) {
    if (!/ERR_CERT_/.test(String(e?.message))) throw e;
  }
  const pin = await readPin(dir);
  if (!pin) return mismatch("没有登记文件 .glkvm_cert_pin", "", "", log);
  const seen = await certFingerprint();
  if (seen !== pin) return mismatch("Node 直连取到的指纹不同", seen, pin, log);
  if (!String(await page.url()).startsWith("chrome-error")) return;   // 已不在拦截页（别处放行过）
  await page.keyboard.type("thisisunsafe");                           // Chromium 拦截页的放行口令
  for (let i = 0; i < 15; i++) {
    await page.waitForTimeout(1000);
    if (!String(await page.url()).startsWith("chrome-error")) break;
  }
  if (String(await page.url()).startsWith("chrome-error")) throw new Error("GLKVM 证书放行后页面仍打不开");
  const bfp = await browserFp(page);
  if (bfp && bfp !== pin) { await page.goto("about:blank").catch(() => {}); return mismatch("浏览器实际接受的证书指纹不同", bfp, pin, log); }
  log?.(`GLKVM 证书授权已按登记指纹续上${bfp ? "（浏览器侧复核一致）" : "（浏览器侧取不到证书，只有 Node 侧核对）"}`);
  await fs.rm(`${RUN}/cert_mismatch.json`, { force: true }).catch(() => {});
}
