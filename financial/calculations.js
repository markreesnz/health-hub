/* Pure calculations used by both Plan and Spending. Dates are calendar days. */
(function(root) {
  function forecast(state, categories, target, startISO, endISO) {
    const excluded = new Set(categories.filter(c => c.excluded || c.oneOff).map(c => c.name));
    const today = new Date(endISO + "T00:00:00Z");
    const baselineStart = new Date(startISO + "T00:00:00Z");
    const daysElapsed = Math.max(1, Math.floor((today - baselineStart) / 86400000) + 1);
    const ANNUAL_DAYS = 365;
    // "Remaining" in the 12-month projection window from baseline; floors at 0
    const daysRemaining = Math.max(0, ANNUAL_DAYS - daysElapsed);
    const fortnightsRemaining = daysRemaining / 14;
    const monthsElapsed = daysElapsed / 30.44;
    const monthsRemaining = daysRemaining / 30.44;

    // Per-category spend since baseline (RAW cash). For the rolling 12-month view we don't
    // care about pre-baseline cash anymore — we're projecting the future, not retro-fitting.
    const catYtd = {};
    (state.transactions||[]).forEach(t => {
      if (excluded.has(t.category) || t.excluded) return;
      if (t.amount >= 0) return;
      if (t.date < startISO || t.date > endISO) return;
      catYtd[t.category] = (catYtd[t.category] || 0) + (-t.amount);
    });

    // Override semantics in a 12-month frame:
    //   year:      1× amount  (one annual payment per 12 months)
    //   quarter:   4× amount
    //   month:    12× amount
    //   fortnight: 26× amount
    // If actual since-baseline spend already exceeds the projection, use that.
    const PERIODS_PER_YEAR = { fortnight: 26, month: 12, quarter: 4, year: 1 };
    const overrides = state.categoryAnnualForecast || {};
    function getOverrideShape(cat) {
      const o = overrides[cat];
      if (o == null) return null;
      if (typeof o === 'number') return { amount: o, period: 'year' };
      return { amount: +o.amount || 0, period: o.period || 'year' };
    }
    function forecastFromOverride(o, ytdAmt) {
      if (o == null) return null;
      const shape = (typeof o === 'number') ? { amount: o, period: 'year' } : { amount: +o.amount, period: o.period || 'year' };
      const amt = +shape.amount;
      if (isNaN(amt) || amt < 0) return null;
      const annualEquivalent = amt * (PERIODS_PER_YEAR[shape.period] || 1);
      return Math.max(ytdAmt, annualEquivalent);
    }
    // Single canonical target — the same $142K figure the Plan/retirement math uses. Account
    // funding sums to ~the same amount; education is a separate pre-funded pot.
    const spendingCategories = categories.filter(c => !c.excluded && !c.oneOff).map(c => c.name);
    const catRows = spendingCategories.map(cat => {
      const ytdAmt = catYtd[cat] || 0;
      const overrideForecast = forecastFromOverride(overrides[cat], ytdAmt);
      const isManual = overrideForecast != null;
      // Auto-inferred forecast: annualize the since-baseline daily rate
      const forecastCat = isManual ? overrideForecast : (ytdAmt * ANNUAL_DAYS / daysElapsed);
      const remaining = Math.max(0, forecastCat - ytdAmt);
      const shape = getOverrideShape(cat);
      return { cat, ytdAmt, forecastCat, remaining, isManual, shape };
    }).sort((a, b) => b.forecastCat - a.forecastCat);

    // Headline totals — sum of per-category forecasts
    const totalYtd = catRows.reduce((s, r) => s + r.ytdAmt, 0);
    const totalForecast = catRows.reduce((s, r) => s + r.forecastCat, 0);
    const totalRemaining = catRows.reduce((s, r) => s + r.remaining, 0);
    const gap = totalForecast - target;
    const avgPerMonth = monthsElapsed > 0 ? totalYtd / monthsElapsed : 0;
    const remainingBudget = target - totalYtd;
    const monthlyBudget = monthsRemaining > 0 ? remainingBudget / monthsRemaining : 0;

    return {startISO, daysElapsed, ANNUAL_DAYS, daysRemaining, fortnightsRemaining, monthsElapsed, monthsRemaining, catRows, totalYtd, totalForecast, totalRemaining, gap, avgPerMonth, remainingBudget, monthlyBudget, target};
  }
  function alignedStart(anchorISO, dateISO) {
    const anchor = Date.parse(anchorISO + 'T00:00:00Z');
    const target = Date.parse(dateISO + 'T00:00:00Z');
    const start = anchor + Math.floor((target - anchor) / (14 * 86400000)) * 14 * 86400000;
    return new Date(start).toISOString().slice(0, 10);
  }
  function dayIndex(startISO, dateISO) {
    return Math.floor((Date.parse(dateISO + 'T00:00:00Z') - Date.parse(startISO + 'T00:00:00Z')) / 86400000) + 1;
  }
  // Observed cash spending, never category forecast overrides. Keep credits visible.
  function drawdown(state, categories, totals, baseline, today) {
    const day = 86400000;
    const end = Date.parse(today + 'T00:00:00Z');
    const yearStart = new Date(end - 364 * day).toISOString().slice(0, 10);
    const dates = (state.transactions || []).map(t => t.date).filter(d => d && d <= today).sort();
    const start = [baseline, yearStart, dates[0] || today].sort().pop();
    const days = Math.max(0, Math.floor((end - Date.parse(start + 'T00:00:00Z')) / day) + 1);
    const excluded = new Set(categories.filter(c => c.excluded || c.oneOff).map(c => c.name));
    let debits = 0, credits = 0, count = 0;
    for (const t of state.transactions || []) {
      if (t.excluded || excluded.has(t.category) || t.date < start || t.date > today) continue;
      const amount = Number(t.amount);
      if (!Number.isFinite(amount)) continue;
      if (amount < 0) { debits -= amount; count++; }
      else credits += amount;
    }
    const actual = debits - credits;
    const annual = days && count ? actual * 365 / days : null;
    const planned = Object.entries(state.accountFortnightly || {}).reduce((sum, [account, value]) =>
      sum + (account === 'Savings' ? 0 : Math.max(0, Number(value) || 0) * 26), 0);
    const costs = Number.isFinite(state.retirementKnownCosts) ? state.retirementKnownCosts : 63875;
    const capital = Math.max(0, totals.total - costs);
    const accessible = Math.max(0, capital - totals.ks);
    const rate = amount => capital > 0 && amount !== null ? amount / capital * 100 : null;
    return {start, today, days, debits, credits, actual, annual, planned, costs, capital, accessible,
      actualRate: rate(annual), plannedRate: rate(planned),
      levels: [2, 2.5, 3, 3.5, 4].map(percent => ({percent, allowance: capital * percent / 100,
        headroom: annual === null ? null : capital * percent / 100 - annual}))};
  }
  const api = {forecast, alignedStart, dayIndex, drawdown};
  if (typeof module !== 'undefined') module.exports = api;
  root.FinanceCalculations = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
