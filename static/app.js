// Site-wide behaviour: toasts, uploads, theme toggle, activity ping, and the
// "open instantly, then refresh" logic for the main tab pages (works with sw.js).
(function () {
  var PAGES = "uv-pages-v1";
  var TAB_PAGES = ["/files/", "/timetable", "/reminders", "/settings"];
  var path = location.pathname;
  var lastToggle = 0;

  function dropPages() {
    if (window.caches) caches.delete(PAGES).catch(function () {});
  }

  // Wire up the parts of the page that can be replaced when fresh content arrives.
  function init(root) {
    // Success messages fade out on their own; errors stay until dismissed.
    root.querySelectorAll(".toast").forEach(function (toast) {
      function close() {
        toast.classList.add("hide");
        setTimeout(function () { toast.remove(); }, 300);
      }
      toast.querySelector("button").addEventListener("click", close);
      if (toast.classList.contains("toast-success")) setTimeout(close, 4000);
    });

    // File pickers marked data-autosubmit upload as soon as files are chosen;
    // an upload bar also accepts files dropped onto it.
    root.querySelectorAll("input[data-autosubmit]").forEach(function (input) {
      input.addEventListener("change", function () {
        if (!input.files.length) return;
        startUpload(input.form);
      });
    });
    root.querySelectorAll("[data-dropzone]").forEach(function (zone) {
      ["dragenter", "dragover"].forEach(function (ev) {
        zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.add("drag"); });
      });
      ["dragleave", "drop"].forEach(function (ev) {
        zone.addEventListener(ev, function () { zone.classList.remove("drag"); });
      });
      zone.addEventListener("drop", function (e) {
        e.preventDefault();
        var input = zone.querySelector("input[type=file]:not([webkitdirectory])");
        if (!e.dataTransfer.files.length) return;
        input.files = e.dataTransfer.files;
        input.dispatchEvent(new Event("change"));
      });
    });
  }
  init(document);

  // ----- uploads: the browser sends files straight to storage (the site's server
  // can't take big request bodies); the server only approves and records them -----
  var uploading = false;

  function postJSON(url, body) {
    return fetch(url, { method: "POST", credentials: "same-origin",
                        headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || "Upload failed"); return j; }); });
  }

  function putFile(url, file, onProgress) {
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open("PUT", url);
      xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
      xhr.upload.onprogress = function (e) { if (e.lengthComputable) onProgress(e.loaded); };
      xhr.onload = function () { xhr.status < 300 ? resolve() : reject(new Error("HTTP " + xhr.status)); };
      xhr.onerror = function () { reject(new Error("network error")); };
      xhr.send(file);
    });
  }

  function startUpload(form) {
    if (uploading) return;
    var files = [];
    form.querySelectorAll("input[type=file]").forEach(function (input) {
      Array.prototype.forEach.call(input.files, function (f) { files.push({ file: f, name: f.webkitRelativePath || f.name }); });
    });
    if (!files.length) { return; }
    var field = function (n) { var el = form.querySelector("[name=" + n + "]"); return el ? el.value : ""; };
    var folder = field("folder"), subject = field("subject");
    var note = document.querySelector(".uploading-note");
    var say = function (t) { if (note) note.textContent = t; };
    uploading = true;
    document.body.classList.add("uploading");
    say("Preparing upload…");

    postJSON("/upload/sign", { folder: folder, subject: subject,
                               files: files.map(function (f) { return { name: f.name, size: f.file.size }; }) })
      .then(function (plan) {
        var failed = plan.errors.slice(), done = 0, loaded = {}, totalBytes = 0, next = 0;
        plan.items.forEach(function (it) { totalBytes += files[it.i].file.size; });
        function progress() {
          var sum = 0; for (var k in loaded) sum += loaded[k];
          say("Uploading " + done + " of " + plan.items.length + (totalBytes ? " · " + Math.round(sum * 100 / totalBytes) + "%" : ""));
        }
        function worker() {
          if (next >= plan.items.length) return Promise.resolve();
          var it = plan.items[next++], f = files[it.i].file;
          return putFile(it.url, f, function (n) { loaded[it.i] = n; progress(); })
            .then(function () { done++; loaded[it.i] = f.size; progress(); })
            .catch(function (e) { failed.push(it.name + " (" + e.message + ")"); })
            .then(worker);
        }
        progress();
        return Promise.all([worker(), worker(), worker()]).then(function () {
          say("Finishing…");
          return postJSON("/upload/done", { folder: plan.folder, uploaded: done, failed: failed });
        });
      })
      .then(function (res) { dropPages(); location.href = res.url; })
      .catch(function (e) {
        uploading = false;
        document.body.classList.remove("uploading");
        alert(e.message || "Upload failed");
      });
  }

  // The top "Upload" panel posts a form: take it over so big files work too.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (form.getAttribute && /\/upload$/.test(form.getAttribute("action") || "") && window.XMLHttpRequest && window.fetch) {
      e.preventDefault();
      e.stopImmediatePropagation();
      startUpload(form);
    }
  }, true);

  // Any form that changes data makes the saved pages out of date.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    var theme = form.querySelector && form.querySelector("[name=theme]:checked, input[type=hidden][name=theme]");
    if (theme) { try { localStorage.setItem("uv_theme", theme.value); } catch (x) {} }
    if (!form.classList || !form.classList.contains("theme-toggle")) dropPages();
  }, true);

  // Checkboxes and file pickers submit their form without a submit event.
  document.addEventListener("change", function (e) {
    if (e.target.form && !e.target.form.classList.contains("theme-toggle")) dropPages();
  }, true);

  // Light/dark toggle: switch instantly, then save the choice in the background.
  (function () {
    var form = document.querySelector(".theme-toggle");
    var root = document.documentElement;
    if (!form) return;
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var current = root.dataset.theme;
      if (current === "system") {
        current = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
      }
      var next = current === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      lastToggle = Date.now();
      try { localStorage.setItem("uv_theme", next); } catch (x) {}
      var data = new FormData();
      data.append("theme", next);
      fetch(form.action, { method: "POST", body: data, credentials: "same-origin",
                           headers: { "X-Requested-With": "fetch" } }).catch(function () {});
    });
  })();

  // While this tab is visible, tell the server every 2 minutes that the user
  // is still here (drives the online dot on the admin page).
  (function () {
    function ping() {
      if (document.visibilityState !== "visible") return;
      fetch("/ping", { method: "POST", credentials: "same-origin" }).catch(function () {});
    }
    setInterval(ping, 120000);
    document.addEventListener("visibilitychange", ping);
  })();

  // ----- instant tab pages -----
  if (TAB_PAGES.indexOf(path) === -1 || !window.caches || !window.DOMParser) return;

  function swapIn(doc) {
    var pairs = [[".content"], [".topbar-main"], [".sidebar .nav"]];
    var typing = document.activeElement && /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName) &&
                 document.activeElement.value;
    pairs.forEach(function (p) {
      var cur = document.querySelector(p[0]), next = doc.querySelector(p[0]);
      if (!cur || !next || cur.innerHTML === next.innerHTML) return;
      if (typing && cur.contains(document.activeElement)) return;  // never wipe what is being typed
      cur.innerHTML = next.innerHTML;
      init(cur);
    });
    var theme = doc.documentElement.dataset.theme;
    if (theme && Date.now() - lastToggle > 8000 && theme !== document.documentElement.dataset.theme) {
      document.documentElement.dataset.theme = theme;
    }
    if (theme) { try { localStorage.setItem("uv_theme", theme); } catch (x) {} }
  }

  function revalidate() {
    return fetch(location.pathname + location.search, { credentials: "same-origin" }).then(function (res) {
      if (res.redirected) {  // signed out (or session over): leave the saved pages behind
        dropPages();
        location.replace(res.url);
        return;
      }
      if (!res.ok) return;
      var copy = res.clone();
      return res.text().then(function (html) {
        swapIn(new DOMParser().parseFromString(html, "text/html"));
        return caches.open(PAGES).then(function (c) { return c.put(location.pathname + location.search, copy); });
      });
    });
  }

  // Warm the other tabs so the first tap on each one is instant too.
  function warmOthers() {
    if (navigator.connection && navigator.connection.saveData) return;
    caches.open(PAGES).then(function (cache) {
      var todo = TAB_PAGES.filter(function (u) { return u !== path; });
      (function next() {
        var u = todo.shift();
        if (!u) return;
        cache.match(u).then(function (hit) {
          if (hit) return;
          return fetch(u, { credentials: "same-origin" }).then(function (res) {
            if (res.ok && !res.redirected) return cache.put(u, res);
          });
        }).catch(function () {}).then(function () { setTimeout(next, 400); });
      })();
    });
  }

  window.addEventListener("load", function () {
    setTimeout(function () {
      if (navigator.onLine === false) return;
      revalidate().catch(function () {}).then(warmOthers);
    }, 50);
  });
})();
