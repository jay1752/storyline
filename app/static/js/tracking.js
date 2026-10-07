// Wires the stock-picker combobox used on /management/add.
// Each picker lives inside a ".track-row" (the add form) — picking a result fills that row's own hidden symbol/company_name
// inputs and fires "stock-picked" so htmx can refresh that row's suggested price.
function selectStock(btn) {
  const row = btn.closest(".track-row");
  if (!row) return;
  const symbol = btn.dataset.symbol || "";
  const name = btn.dataset.name || "";

  const symbolInput = row.querySelector('input[name="symbol"]');
  const nameInput = row.querySelector('input[name="company_name"]');
  if (symbolInput) symbolInput.value = symbol;
  if (nameInput) nameInput.value = name;

  const label = row.querySelector(".picker-label");
  if (label) label.textContent = symbol ? symbol + " — " + name : "No stock selected yet";

  const query = row.querySelector(".picker-query");
  if (query) query.value = "";

  const results = row.querySelector(".stock-search-results");
  if (results) results.innerHTML = "";

  // Only auto-suggest a price when the field is still empty. On the upload-review
  // table the price usually already came straight from the statement (the real
  // transaction price, better than any DB estimate) — fixing a wrong symbol match
  // shouldn't silently clobber it. The "DB price" button next to the field still
  // refetches on demand regardless of what's currently there.
  const priceInput = row.querySelector('input[name="price"]');
  if (!priceInput || !priceInput.value) {
    row.dispatchEvent(new CustomEvent("stock-picked", { bubbles: true }));
  }
}
