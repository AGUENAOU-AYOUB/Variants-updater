// hide loader when server wakes
fetch("/__ping").finally(() => document.getElementById("loader").classList.add("hide"));
// keep dyno awake
setInterval(() => fetch("/__ping").catch(() => { }), 15000);
// show toasts
document.querySelectorAll(".toast").forEach(t => new bootstrap.Toast(t).show());

// wire Update button
console.log("✅ script.js has been loaded and is executing");
document.getElementById("run")?.addEventListener("click", () => {
    console.log("🔔 Update button clicked");
    fetch("/update", { method: "POST" })
        .then(resp => console.log("🔄 /update response status:", resp.status))
        .then(() => location.reload())
        .catch(err => console.error("🚨 Fetch error:", err));
});

// live logs on prices page
const box = document.getElementById("logs");
if (box) {
    const es = new EventSource("/stream");
    es.onmessage = e => {
        box.textContent += e.data + "\n";
        box.scrollTop = box.scrollHeight;
    };
}
