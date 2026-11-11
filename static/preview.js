// File preview: PDF, Word, Excel, images and text, with zoom and a full-screen mode.
(function () {
  var box = document.getElementById("pv");
  if (!box) return;
  var mode = box.dataset.mode, src = box.dataset.src;
  var stage = box.querySelector(".pv-stage");
  var doc = box.querySelector(".pv-doc");
  var level = box.querySelector(".pv-level");
  var count = box.querySelector(".pv-count");
  var fullBtn = box.querySelector('[data-z="full"]');
  var scale = 1, MIN = 0.5, MAX = 4;
  var applyZoom = function () {};

  function setScale(s) {
    scale = Math.min(MAX, Math.max(MIN, Math.round(s * 100) / 100));
    level.textContent = Math.round(scale * 100) + "%";
    applyZoom();
  }

  box.querySelectorAll("[data-z]").forEach(function (b) {
    b.addEventListener("click", function () {
      var a = b.dataset.z;
      if (a === "in") setScale(scale * 1.25);
      else if (a === "out") setScale(scale / 1.25);
      else if (a === "reset") setScale(1);
      else if (a === "full") toggleFull();
    });
  });

  // Full screen: a page-sized overlay works on every device (iPhones can't
  // fullscreen arbitrary elements); the real Fullscreen API is added where it exists.
  function toggleFull(force) {
    var on = typeof force === "boolean" ? force : !box.classList.contains("is-full");
    box.classList.toggle("is-full", on);
    document.body.classList.toggle("pv-lock", on);
    fullBtn.querySelector(".pv-full-label").textContent = on ? "Close" : "Full screen";
    try {
      if (on && box.requestFullscreen && !document.fullscreenElement) box.requestFullscreen().catch(function () {});
      if (!on && document.fullscreenElement) document.exitFullscreen();
    } catch (e) {}
    applyZoom();
  }
  document.addEventListener("fullscreenchange", function () {
    if (!document.fullscreenElement && box.classList.contains("is-full")) toggleFull(false);
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && box.classList.contains("is-full")) toggleFull(false);
  });

  function fail(msg) {
    doc.className = "pv-doc pv-error";
    doc.innerHTML = "";
    var p = document.createElement("p");
    p.textContent = msg + " ";
    if (src) {
      var a = document.createElement("a");
      a.href = src; a.target = "_blank"; a.rel = "noopener"; a.textContent = "Open it in a new tab";
      p.appendChild(a);
    }
    doc.appendChild(p);
  }

  function fetchBuffer() {
    return fetch(src).then(function (r) {
      if (!r.ok) throw new Error("download failed");
      return r.arrayBuffer();
    });
  }

  // ----- images: width follows the zoom, the stage scrolls -----
  if (mode === "image") {
    var img = box.querySelector(".pv-img");
    applyZoom = function () { img.style.width = (scale * 100) + "%"; };
    applyZoom();
  }

  // ----- plain text: font size follows the zoom -----
  if (mode === "text") {
    applyZoom = function () { doc.style.fontSize = (13 * scale) + "px"; };
  }

  // ----- PDF (pdf.js): pages drawn to canvases, sharp at any zoom -----
  if (mode === "pdf") {
    var pdfjs = window.pdfjsLib;
    if (!pdfjs) { fail("The PDF viewer could not load."); return; }
    pdfjs.GlobalWorkerOptions.workerSrc = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";
    var pages = [], timer;
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) { if (en.isIntersecting) draw(en.target.__page); });
    }, { root: stage, rootMargin: "600px 0px" });

    var pageWidth = function () { return Math.max(200, (stage.clientWidth - 24) * scale); };

    function draw(pg) {
      var w = pageWidth();
      if (pg.drawnWidth === w || pg.busy) return;
      pg.busy = true;
      var vp0 = pg.page.getViewport({ scale: 1 });
      var vp = pg.page.getViewport({ scale: (w / vp0.width) * Math.min(window.devicePixelRatio || 1, 2.5) });
      var canvas = document.createElement("canvas");
      canvas.width = vp.width; canvas.height = vp.height;
      pg.page.render({ canvasContext: canvas.getContext("2d"), viewport: vp }).promise.then(function () {
        canvas.style.width = w + "px";
        canvas.style.height = (w * vp0.height / vp0.width) + "px";
        pg.holder.innerHTML = "";
        pg.holder.appendChild(canvas);
        pg.drawnWidth = w; pg.busy = false;
        if (pg.drawnWidth !== pageWidth()) draw(pg);  // zoomed again while drawing
      }).catch(function () { pg.busy = false; });
    }

    applyZoom = function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        var w = pageWidth();
        pages.forEach(function (pg) {
          var vp0 = pg.page.getViewport({ scale: 1 });
          var h = (w * vp0.height / vp0.width) + "px";
          pg.holder.style.width = w + "px"; pg.holder.style.height = h;
          var c = pg.holder.firstChild;
          if (c && c.tagName === "CANVAS") { c.style.width = w + "px"; c.style.height = h; }
          var r = pg.holder.getBoundingClientRect(), s = stage.getBoundingClientRect();
          if (r.bottom > s.top - 600 && r.top < s.bottom + 600) draw(pg);
        });
      }, 120);
    };
    window.addEventListener("resize", applyZoom);

    pdfjs.getDocument({ url: src }).promise.then(function (pdf) {
      doc.className = "pv-doc pv-pages"; doc.innerHTML = "";
      count.textContent = pdf.numPages + (pdf.numPages === 1 ? " page" : " pages");
      var chain = Promise.resolve();
      for (var i = 1; i <= pdf.numPages; i++) {
        (function (n) {
          chain = chain.then(function () { return pdf.getPage(n); }).then(function (page) {
            var holder = document.createElement("div");
            holder.className = "pv-page";
            doc.appendChild(holder);
            var pg = { page: page, holder: holder };
            holder.__page = pg;
            var vp0 = page.getViewport({ scale: 1 }), w = pageWidth();
            holder.style.width = w + "px";
            holder.style.height = (w * vp0.height / vp0.width) + "px";
            pages.push(pg);
            observer.observe(holder);
          });
        })(i);
      }
    }).catch(function () { fail("This PDF could not be shown here."); });
  }

  // ----- Word (.docx): converted to HTML in the browser; nothing leaves your device -----
  if (mode === "docx") {
    applyZoom = function () { doc.style.fontSize = (16 * scale) + "px"; };
    if (!window.mammoth) { fail("The Word viewer could not load."); return; }
    fetchBuffer().then(function (buf) { return mammoth.convertToHtml({ arrayBuffer: buf }); }).then(function (res) {
      var parsed = new DOMParser().parseFromString(res.value, "text/html");
      parsed.querySelectorAll("script, iframe, object, embed, style, link").forEach(function (n) { n.remove(); });
      parsed.querySelectorAll("*").forEach(function (n) {
        Array.from(n.attributes).forEach(function (a) {
          if (/^on/i.test(a.name) || (/^(href|src)$/i.test(a.name) && /^\s*javascript:/i.test(a.value))) n.removeAttribute(a.name);
        });
      });
      doc.className = "pv-doc pv-word";
      doc.innerHTML = parsed.body.innerHTML || "<p>This document is empty.</p>";
      applyZoom();
    }).catch(function () { fail("This Word file could not be shown here."); });
  }

  // ----- Excel (.xlsx / .xls): each sheet as a table -----
  if (mode === "sheet") {
    applyZoom = function () { doc.style.fontSize = (13 * scale) + "px"; };
    if (!window.XLSX) { fail("The spreadsheet viewer could not load."); return; }
    fetchBuffer().then(function (buf) {
      var wb = XLSX.read(buf, { type: "array" });
      doc.className = "pv-doc pv-sheet";
      doc.innerHTML = "";
      var tabs = document.createElement("div"); tabs.className = "pv-tabs";
      var body = document.createElement("div"); body.className = "pv-sheet-body";
      function show(name, btn) {
        body.innerHTML = XLSX.utils.sheet_to_html(wb.Sheets[name], { header: "", footer: "" });
        tabs.querySelectorAll("button").forEach(function (b) { b.classList.toggle("on", b === btn); });
      }
      wb.SheetNames.forEach(function (name, i) {
        var b = document.createElement("button");
        b.type = "button"; b.textContent = name;
        b.addEventListener("click", function () { show(name, b); });
        tabs.appendChild(b);
        if (i === 0) show(name, b);
      });
      if (wb.SheetNames.length > 1) doc.appendChild(tabs);
      doc.appendChild(body);
      applyZoom();
    }).catch(function () { fail("This spreadsheet could not be shown here."); });
  }
})();
