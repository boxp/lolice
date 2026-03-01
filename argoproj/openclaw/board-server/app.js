(function() {
  "use strict";
  var contentEl = document.getElementById("content");
  var statusEl = document.getElementById("status");
  var lastContent = "";

  function fetchBoard() {
    fetch("/api/board.md")
      .then(function(res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.text();
      })
      .then(function(md) {
        if (md !== lastContent) {
          lastContent = md;
          var html = marked.parse(md);
          var clean = DOMPurify.sanitize(html, {
            ALLOWED_TAGS: [
              "h1","h2","h3","h4","h5","h6","p","ul","ol","li",
              "a","strong","em","code","pre","blockquote","table","thead",
              "tbody","tr","th","td","br","hr","del","input","span","div",
              "img","details","summary","dl","dt","dd","sup","sub"
            ],
            ALLOWED_ATTR: [
              "href","src","alt","title","class","id","type",
              "checked","disabled","target","rel"
            ],
            ALLOW_DATA_ATTR: false
          });
          contentEl.innerHTML = clean;
        }
        statusEl.textContent = "Updated: " + new Date().toLocaleTimeString();
        statusEl.className = "status ok";
      })
      .catch(function(err) {
        statusEl.textContent = "Error: " + err.message;
        statusEl.className = "status error";
      });
  }

  fetchBoard();
  setInterval(fetchBoard, 5000);
})();
