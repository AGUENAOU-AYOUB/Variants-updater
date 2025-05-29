/* hide loader once dyno responds */
fetch("/__ping").finally(() => document.getElementById("loader").classList.add("hide"));

/* keep Render free dyno awake */
setInterval(() => fetch("/__ping").catch(() => { }), 15000);

/* Bootstrap toasts auto-show */
document.querySelectorAll(".toast").forEach(t => new bootstrap.Toast(t).show());

/* bulk update button */
document.getElementById("run")?.addEventListener("click", () => {
    fetch("/update", { method: "POST" }).then(() => location.reload());
});

/* live logs on prices page */
const box = document.getElementById("logs");
if (box) {
    const es = new EventSource("/stream");
    es.onmessage = e => {
        box.textContent += e.data + "\n";
        box.scrollTop = box.scrollHeight;
    };
}
