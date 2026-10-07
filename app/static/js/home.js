// Homepage: India / US switch for the live sample card, and a shadow on the nav once the page scrolls.
(function () {
  const tabs = document.querySelectorAll("#demo-tabs button");
  const cards = document.querySelectorAll("[data-demo-card]");
  tabs.forEach((b) => b.addEventListener("click", () => {
    tabs.forEach((x) => x.classList.toggle("active", x === b));
    cards.forEach((c) => { c.hidden = c.dataset.demoCard !== b.dataset.demo; });
  }));

  const nav = document.querySelector(".hm-nav");
  if (nav) {
    const on = () => nav.classList.toggle("scrolled", window.scrollY > 8);
    on();
    window.addEventListener("scroll", on, { passive: true });
  }
})();
