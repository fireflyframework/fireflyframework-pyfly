/* PyFly landing — interactions: nav, copy, tabs, fireflies, scroll reveal.
   Vanilla JS, no dependencies. */
(function () {
  "use strict";

  /* ---- sticky nav background on scroll ---- */
  var nav = document.getElementById("nav");
  function onScroll() {
    if (!nav) return;
    nav.classList.toggle("is-scrolled", window.scrollY > 8);
  }
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  /* ---- mobile menu ---- */
  var burger = document.querySelector(".nav__burger");
  if (burger && nav) {
    burger.addEventListener("click", function () {
      var open = nav.classList.toggle("is-open");
      burger.setAttribute("aria-expanded", String(open));
    });
    nav.querySelectorAll(".nav__links a").forEach(function (a) {
      a.addEventListener("click", function () {
        nav.classList.remove("is-open");
        burger.setAttribute("aria-expanded", "false");
      });
    });
  }

  /* ---- copy to clipboard ---- */
  function flash(btn, label) {
    var original = label ? btn.querySelector(label) : null;
    var prev = original ? original.textContent : btn.textContent;
    btn.classList.add("is-copied");
    if (original) original.textContent = "Copied!";
    else btn.textContent = "✓";
    setTimeout(function () {
      btn.classList.remove("is-copied");
      if (original) original.textContent = prev;
      else btn.textContent = prev;
    }, 1600);
  }
  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) { /* ignore */ }
    document.body.removeChild(ta);
    return Promise.resolve();
  }
  document.querySelectorAll("[data-copy-text]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      copyText(btn.getAttribute("data-copy-text")).then(function () {
        flash(btn, btn.classList.contains("install__copy") ? ".install__copy-label" : null);
      });
    });
  });

  /* ---- code tabs ---- */
  var tabs = document.querySelectorAll(".code-tab");
  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      var key = tab.getAttribute("data-tab");
      tabs.forEach(function (t) {
        var active = t === tab;
        t.classList.toggle("is-active", active);
        t.setAttribute("aria-selected", String(active));
      });
      document.querySelectorAll(".code-body").forEach(function (body) {
        body.classList.toggle("is-active", body.getAttribute("data-pane") === key);
      });
    });
  });

  /* ---- fireflies ---- */
  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  function rand(min, max) { return Math.random() * (max - min) + min; }
  function spawnFireflies(container, count) {
    if (!container) return;
    var frag = document.createDocumentFragment();
    for (var i = 0; i < count; i++) {
      var f = document.createElement("span");
      f.className = "firefly";
      var size = rand(3, 7);
      f.style.width = size + "px";
      f.style.height = size + "px";
      f.style.left = rand(2, 96) + "%";
      f.style.top = rand(6, 92) + "%";
      f.style.setProperty("--d", rand(9, 20).toFixed(1) + "s");
      f.style.setProperty("--delay", "-" + rand(0, 12).toFixed(1) + "s");
      f.style.setProperty("--dx", rand(-40, 40).toFixed(0) + "px");
      f.style.setProperty("--dy", rand(-90, -30).toFixed(0) + "px");
      f.style.setProperty("--peak", rand(0.55, 0.95).toFixed(2));
      frag.appendChild(f);
    }
    container.appendChild(frag);
  }
  if (!reduceMotion) {
    spawnFireflies(document.getElementById("fireflies"), 26);
    spawnFireflies(document.getElementById("fireflies2"), 14);
  } else {
    spawnFireflies(document.getElementById("fireflies"), 10);
  }

  /* ---- scroll reveal ---- */
  var revealTargets = document.querySelectorAll(
    ".why-card, .phil-card, .pat-card, .figure, .layer, .step, .code-panel, .book__text, .book__art, .section__title, .lede"
  );
  if ("IntersectionObserver" in window && !reduceMotion) {
    revealTargets.forEach(function (el) { el.classList.add("reveal"); });
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("in");
          io.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12, rootMargin: "0px 0px -40px 0px" });
    revealTargets.forEach(function (el) { io.observe(el); });
  }

  /* ---- active nav link on scroll ---- */
  var sections = ["why", "code", "architecture", "patterns", "modules"]
    .map(function (id) { return document.getElementById(id); })
    .filter(Boolean);
  var navLinks = {};
  document.querySelectorAll('.nav__links a[href^="#"]').forEach(function (a) {
    navLinks[a.getAttribute("href").slice(1)] = a;
  });
  if ("IntersectionObserver" in window && sections.length) {
    var spy = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        var link = navLinks[entry.target.id];
        if (link && entry.isIntersecting) {
          Object.keys(navLinks).forEach(function (k) { navLinks[k].classList.remove("is-current"); });
          link.classList.add("is-current");
        }
      });
    }, { threshold: 0.5 });
    sections.forEach(function (s) { spy.observe(s); });
  }
})();
