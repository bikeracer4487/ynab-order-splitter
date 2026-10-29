// YNABuddy -- download your Target in-store receipts as JSON.
//
// Target blocks sign-in from automated browsers, so YNABuddy doesn't scrape
// Target. Instead you run this in your own browser, signed in as usual; it
// calls the same purchase-history endpoints the page itself calls.
//
//   1. Sign in at target.com and open https://www.target.com/orders
//      (the "Purchase history" tab).
//   2. Open DevTools (Cmd+Option+J on Mac, Ctrl+Shift+J elsewhere), click
//      the Console tab, paste this whole file and press Enter. The first
//      time, Chrome asks you to type "allow pasting" before it accepts.
//   3. A file target-receipts-<date>.json downloads. Move it into
//      plugins/target.com/ and run YNABuddy.
//
// Change PAGES to reach further back (10 receipts a page, newest first).

(async () => {
  const PAGE_SIZE = 10; // the page's own size; larger pages are refused
  const PAGES = 10;

  // The page's own public web API key (also in its request URLs).
  const seen = performance.getEntriesByType("resource").map((e) => e.name).find((u) => /[?&]key=[0-9a-f]{40}/.test(u));
  const key = seen ? seen.match(/[?&]key=([0-9a-f]{40})/)[1] : "ff457966e64d5e877fdbad070f276d18ecec4a01";

  const get = async (url) => {
    const r = await fetch(url, { credentials: "include", headers: { accept: "application/json", "x-api-key": key } });
    if (!r.ok) throw new Error(`HTTP ${r.status} for ${url}: ${(await r.text()).slice(0, 300)}`);
    return r.json();
  };
  const pause = (ms) => new Promise((done) => setTimeout(done, ms));

  const history = [];
  const ids = [];
  for (let page = 1; page <= PAGES; page++) {
    const data = await get(`https://api.target.com/guest_order_aggregations/v1/order_history?page_number=${page}&page_size=${PAGE_SIZE}&order_purchase_type=STORE&pending_order=true&shipt_status=true`);
    history.push(data);
    const onPage = new Set(JSON.stringify(data).match(/\b\d{4}-\d{4}-\d{4}-\d{4}\b/g) || []);
    onPage.forEach((id) => { if (!ids.includes(id)) ids.push(id); });
    console.log(`History page ${page}: ${onPage.size} receipts`);
    if (onPage.size < PAGE_SIZE) break;
    await pause(300);
  }

  const receipts = {};
  for (const [i, id] of ids.entries()) {
    try {
      receipts[id] = await get(`https://api.target.com/post_orders/v1/orders/${id}/store`);
    } catch (e) {
      receipts[id] = { error: String(e) };
    }
    console.log(`Receipt ${i + 1} of ${ids.length}: ${id}`);
    await pause(300);
  }

  const out = { downloaded_at: new Date().toISOString(), history, receipts };
  const now = new Date();
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  const a = document.createElement("a");
  // octet-stream, not json: some pages drop the download and open json in a tab
  a.href = URL.createObjectURL(new Blob([JSON.stringify(out, null, 2)], { type: "application/octet-stream" }));
  a.download = `target-receipts-${today}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  console.log(`Done: ${ids.length} receipts saved to ${a.download}`);
})();
