// YNABuddy -- download your Walmart in-store purchases as JSON.
//
// Walmart blocks sign-in from automated browsers, so YNABuddy doesn't scrape
// Walmart. Instead you run this in your own browser, signed in as usual. It
// records the purchase data the page itself loads, and asks Walmart for the
// rest with the very same request the page used.
//
//   1. Sign in at walmart.com and open https://www.walmart.com/orders.
//   2. Open DevTools (Cmd+Option+J on Mac, Ctrl+Shift+J elsewhere), click
//      the Console tab, paste this whole file and press Enter. The first
//      time, Chrome asks you to type "allow pasting" before it accepts.
//   3. It asks you to click "View details" on one in-store purchase and
//      come back with the browser's Back button. Do that, then paste the
//      file again. If you want older purchases, change the date filter or
//      click "Show more" first: the script keeps everything the page loads.
//   4. A file walmart-receipts-<date>.json downloads. Move it into
//      plugins/walmart.com/ and run YNABuddy.
//
// If Walmart refuses the script's requests, it says so and tells you which
// purchases to open by hand; the script records them as you do, and the next
// paste downloads the lot.

(async () => {
  const STORE = "ynabuddy_walmart";
  const saved = JSON.parse(sessionStorage.getItem(STORE) || '{"request": null, "details": {}, "lists": []}');
  const persist = () => sessionStorage.setItem(STORE, JSON.stringify(saved));
  const orderIdOf = (url) => JSON.parse(new URL(url, location.origin).searchParams.get("variables")).orderId;

  // Record the page's own purchase-history and order-detail requests.
  if (!window.__ynabuddyHooked) {
    window.__ynabuddyHooked = true;
    const realFetch = window.fetch;
    window.fetch = async function (input, init) {
      const response = await realFetch.call(this, input, init);
      try {
        const url = typeof input === "string" ? input : input.url;
        if (url.includes("/orchestra/orders/graphql/getOrder/")) {
          const headers = {};
          new Headers((init && init.headers) || (input instanceof Request ? input.headers : {})).forEach((v, k) => { headers[k] = v; });
          saved.request = { url, headers };
          if (response.ok) saved.details[orderIdOf(url)] = await response.clone().json();
          persist();
        } else if (url.includes("/graphql/PurchaseHistory") && response.ok) {
          saved.lists.push(await response.clone().json());
          persist();
        }
      } catch (e) {
        console.warn("YNABuddy: could not record a request", e);
      }
      return response;
    };
  }

  // Every purchase listed: the ones rendered with the page plus any the
  // page loaded since (date filter, "Show more").
  const pageData = JSON.parse(document.getElementById("__NEXT_DATA__").textContent);
  const lists = [pageData.props.pageProps.phRedesignInitialData.data.purchaseHistory, ...saved.lists.map((l) => l.data.purchaseHistory)];
  const listed = {};
  lists.forEach((l) => (l.orders || []).forEach((o) => { listed[o.id] = o; }));
  const inStore = Object.values(listed).filter((o) => o.isInStore);
  console.log(`${Object.keys(listed).length} purchases listed, ${inStore.length} in store`);

  if (!saved.request) {
    console.warn('YNABuddy: now click "View details" on one in-store purchase, come back with the Back button, and paste this again.');
    return;
  }

  // Ask for the in-store purchases the page hasn't loaded, with the page's
  // own request, slowly.
  const pause = (ms) => new Promise((done) => setTimeout(done, ms));
  const missing = inStore.filter((o) => !saved.details[o.id]);
  const base = saved.request.url.split("?")[0];
  const template = JSON.parse(new URL(saved.request.url, location.origin).searchParams.get("variables"));
  let refused = false;
  for (const [i, o] of missing.entries()) {
    const variables = { ...template, orderId: o.id, orderIsInStore: true };
    const response = await fetch(`${base}?variables=${encodeURIComponent(JSON.stringify(variables))}`, { credentials: "include", headers: saved.request.headers });
    console.log(`Purchase ${i + 1} of ${missing.length}: ${o.id} -> HTTP ${response.status}`);
    if (!response.ok) { refused = true; break; }
    saved.details[o.id] = await response.json();
    persist();
    await pause(1500);
  }

  const still = inStore.filter((o) => !saved.details[o.id]);
  if (refused) {
    console.warn(`YNABuddy: Walmart refused the request. Open these ${still.length} purchase(s) by hand (View details, then Back), then paste this again:`);
    still.forEach((o) => console.warn(`   ${o.title || o.orderDate}  /orders/${o.id}?storePurchase=true`));
  }

  const out = { downloaded_at: new Date().toISOString(), history: Object.values(listed), details: saved.details };
  const now = new Date();
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  const a = document.createElement("a");
  // octet-stream, not json: some pages drop the download and open json in a tab
  a.href = URL.createObjectURL(new Blob([JSON.stringify(out, null, 2)], { type: "application/octet-stream" }));
  a.download = `walmart-receipts-${today}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  console.log(`Done: ${inStore.length - still.length} of ${inStore.length} in-store purchases with details saved to ${a.download}`);
})();
