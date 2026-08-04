// 무신사 상품 페이지에서 **화면에 표시된 값**을 읽는다 (D48).
//
// 왜 브라우저인가: 조회수·누적판매·월간 랭킹·문의 수는 서버 HTML에 없다.
// 원시 정수를 주는 API는 goods-detail.musinsa.com에 있는데 그 호스트는
// robots가 `Disallow: /`다 — 쓰지 않는다. **허용된 www 페이지가 화면에
// 보여주는 것만** 읽는다(구간 표기 그대로. 정수로 위조하지 않는다).
//
//   node musinsa_page_scrape.js ids.txt out.json [동시수]
const path = require("path");
const fs = require("fs");
const npxRoot = "/Users/2000atelier/.npm/_npx";
const pkg = fs.readdirSync(npxRoot).map(d => path.join(npxRoot, d, "node_modules/playwright"))
                                   .find(p => fs.existsSync(p));
const { chromium } = require(pkg);

const UA = "Claude-User/1.0 (+https://anthropic.com/claude-user)";
const [, , idsFile, outFile, concArg] = process.argv;
const CONC = parseInt(concArg || "4", 10);
const ids = fs.readFileSync(idsFile, "utf8").trim().split(/\s+/).filter(Boolean);

// 화면 문구 → 값. **정수로 만들지 않는다** — 원문 그대로 담고 분석 층이 밴드로 쓴다.
function extract() {
  const t = document.body.innerText;
  const one = (re) => { const m = t.match(re); return m ? m[0].replace(/\s+/g, " ").trim() : null; };
  const after = (label, re) => {
    const i = t.indexOf(label);
    if (i < 0) return null;
    const m = t.slice(i, i + 60).match(re);
    return m ? m[0].replace(/\s+/g, " ").trim() : null;
  };
  return {
    view_count_display: after("조회수", /[\d.,]+\s*[천만억]?\s*회\s*이상[^\n]*/),
    purchase_count_display: after("누적판매", /[\d.,]+\s*[천만억]?\s*개\s*이상/),
    qna_count: (() => { const m = t.match(/문의\s*([\d,]+)/); return m ? parseInt(m[1].replace(/,/g, ""), 10) : null; })(),
    review_count_screen: (() => { const m = t.match(/후기\s*([\d,]+)/); return m ? parseInt(m[1].replace(/,/g, ""), 10) : null; })(),
    style_no: after("품번", /[A-Z0-9]{6,}/),
    gender_screen: after("성별", /성별\s*(남|여|공용)/),
    season_screen: after("시즌", /시즌\s*[^\n]{0,20}/),
    monthly_rank: (t.match(/\d{4}년\s*\d{1,2}월[^\n]{0,30}위/g) || []).slice(0, 12),
    sold_out_screen: /품절|재입고 알림/.test(t) || null,
  };
}

(async () => {
  // **시스템 Chrome을 쓴다** — playwright 번들 브라우저가 이 환경에 안 깔려 있고
  // (`npx playwright install` 미실행), MCP 서버도 `channel: chrome`으로 돈다.
  const browser = await chromium.launch({ channel: "chrome" });
  const ctx = await browser.newContext({ userAgent: UA });
  const items = [];
  let done = 0, failed = 0;

  async function worker() {
    const page = await ctx.newPage();
    // 이미지·폰트는 안 받는다 — 값만 필요하고, 사이트 부담도 줄인다
    await page.route("**/*", r => ["image", "font", "media"].includes(r.request().resourceType())
      ? r.abort() : r.continue());
    while (true) {
      const id = ids.shift();
      if (!id) break;
      try {
        await page.goto("https://www.musinsa.com/products/" + id,
                        { waitUntil: "domcontentloaded", timeout: 30000 });
        // **"조회수"만 기다린다.** 전에는 `|| "후기"`를 붙였는데 후기가 먼저 떠서
        // 정보 탭이 뜨기 전에 통과했다 — 조회수·문의·품번이 통째로 null이 됐다.
        // 정보 탭은 아래쪽이라 스크롤로 lazy-load를 깨운다.
        await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight)).catch(() => {});
        await page.waitForFunction(() => document.body.innerText.includes("조회수"),
                                   { timeout: 15000 }).catch(() => {});
        const rec = await page.evaluate(extract);
        rec.product_id = id;
        items.push(rec);
      } catch (e) {
        failed++;
        items.push({ product_id: id, _error: String(e).slice(0, 120) });
      }
      if (++done % 25 === 0) process.stderr.write(`  ${done}/${ids.length + done}\n`);
    }
    await page.close();
  }
  await Promise.all(Array.from({ length: CONC }, worker));
  await browser.close();

  const now = new Date().toISOString().slice(0, 19).replace("T", " ");
  const got = items.filter(i => i.view_count_display).length;
  fs.writeFileSync(outFile, JSON.stringify({
    meta: { site: "musinsa", story: "page-scrape", collected_at: now, source: "html",
            notes: "www 화면 표시값. 조회수·누적판매는 구간 표기 원문(정수 아님 — D48)" },
    items }, null, 1));
  console.log(`수집 ${items.length}건 · 조회수 구간 ${got}건(${(100*got/items.length).toFixed(0)}%) · 실패 ${failed}`);
  console.log(`저장: ${outFile}`);
})();
