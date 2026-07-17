console.log("ENZO chat loaded");

let hrToken = "";
let pendingActionContext = null;
let pendingAttachment = null;   // persists a chosen file across picker steps

// Reusable, nicely-styled file-attach UI (used by the date & reason pickers).
function attachmentRowHTML(id) {
    return `
        <div class="attach-row" style="margin-top:14px;">
            <div style="font-size:13px;font-weight:600;color:#475467;margin-bottom:7px;display:flex;align-items:center;gap:6px;">
                <span>📎</span> Attach a document
                <span style="font-weight:400;color:#98a2b3;">(optional)</span>
            </div>
            <label for="${id}_file" id="${id}_drop"
                   style="display:flex;align-items:center;gap:11px;padding:11px 13px;
                          border:1.5px dashed #cbd5e1;border-radius:12px;background:#f8fafc;
                          cursor:pointer;transition:border-color .15s,background .15s;">
                <span id="${id}_ficon"
                      style="flex:none;width:34px;height:34px;border-radius:9px;background:#eff6ff;
                             display:flex;align-items:center;justify-content:center;font-size:16px;">⬆</span>
                <span style="display:flex;flex-direction:column;min-width:0;">
                    <span id="${id}_fname"
                          style="font-size:13px;font-weight:600;color:#1e293b;white-space:nowrap;
                                 overflow:hidden;text-overflow:ellipsis;">Choose a file</span>
                    <span style="font-size:11px;color:#98a2b3;">PNG, JPG or PDF · up to 5 MB</span>
                </span>
            </label>
            <input type="file" id="${id}_file"
                   accept=".png,.jpg,.jpeg,.pdf,image/png,image/jpeg,application/pdf"
                   onchange="handleLeaveAttachment('${id}', this)" style="display:none;">
            <div id="${id}_fstatus" style="font-size:12px;margin-top:6px;"></div>
        </div>`;
}
let pendingConfirm = null;
let currentReader = null;
let isStreaming = false;
let isActionFlow = false;

const CHAT_ENDPOINT = "/chat";

const STATUS_MESSAGES = {
    employee: ["Searching employee records...", "Fetching profile data...", "Checking employee database..."],
    leave: ["Checking leave balances...", "Fetching leave structure...", "Calculating available days..."],
    leave_history: ["Retrieving leave history...", "Fetching leave records...", "Loading leave requests..."],
    apply: ["Checking leave balance...", "Validating dates...", "Applying leave request..."],
    approve: ["Processing approval...", "Updating leave status..."],
    reject: ["Processing rejection...", "Restoring balance..."],
    cancel: ["Processing cancellation...", "Restoring balance..."],
    default: ["Thinking...", "Understanding your request...", "Checking records...", "Preparing response..."]
};

// ---------------------------------------------------------------------------
// AUTH / GREETING
// ---------------------------------------------------------------------------
function decodeUserName(token) {
    // Read the name claim from the JWT payload (no verification needed here —
    // we only use it to greet the user). Falls back to "" if anything fails.
    try {
        const part = (token || "").split(".")[1];
        if (!part) return "";
        const b64 = part.replace(/-/g, "+").replace(/_/g, "/");
        const json = JSON.parse(decodeURIComponent(escape(atob(b64))));
        return (
            json.name || json.fullName || json.FullName || json.full_name ||
            json.unique_name || json.given_name || json.userName ||
            json.UserName || json.preferred_username || json.sub || ""
        ).toString().trim();
    } catch (e) {
        return "";
    }
}

function renderGreeting(name) {
    const greeting = byId("greeting");
    if (!greeting) return;
    const safe = name ? escapeHtml(name) : "";
    greeting.innerHTML =
        `<span class="message-kicker">ENZO</span>` +
        (safe
            ? `Hi ${safe} 👋 I'm ENZO, your HR assistant. Ask me about leave or employees.`
            : `Hi 👋 I'm ENZO, your HR assistant. Ask me about leave or employees.`);
}

function setStatus(state, text) {
    const pill = byId("statusPill");
    const dot = byId("statusDot");
    const txt = byId("statusText");
    if (txt) txt.textContent = text;
    if (state === "unauth") {
        if (pill) { pill.style.background = "#FEE4E2"; pill.style.color = "#B42318"; }
        if (dot) { dot.style.background = "#F04438"; }
    } else {
        if (pill) { pill.style.background = ""; pill.style.color = ""; }
        if (dot) { dot.style.background = ""; }
    }
}

function setUnauthorized() {
    setStatus("unauth", "Unauthorised");
    const msg = makeMessage("bot-message error-message",
        "🔒 Your session has expired. Please log in again to continue.");
    appendMessage(msg);
}

function isAuthError(response) {
    return response && (response.status === 401 || response.status === 403);
}

window.addEventListener("message", (event) => {
    if (event.data && event.data.token) {
        hrToken = event.data.token;
        console.log("Token received");
        const name = (event.data.name || event.data.userName ||
            decodeUserName(hrToken) || "").toString().trim();
        // greet with the first name for a clean, friendly line
        const first = name ? name.split(/\s+/)[0] : "";
        const pretty = first ? first.charAt(0).toUpperCase() + first.slice(1).toLowerCase() : "";
        renderGreeting(pretty);
    }
});

function byId(id) {
    return document.getElementById(id);
}

function chatBox() {
    return byId("chatBox");
}

function scrollToBottom() {
    const box = chatBox();
    box.scrollTop = box.scrollHeight;
}

function escapeHtml(value) {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

function makeMessage(className, text = "") {
    const el = document.createElement("div");
    el.className = className;
    if (text) el.textContent = text;
    return el;
}

function appendMessage(el) {
    chatBox().appendChild(el);
    scrollToBottom();
    return el;
}

function blockInput() {
    isActionFlow = true;
    byId("messageInput").disabled = true;
    byId("inputBlocker").classList.add("active");
    byId("voiceBtn").disabled = true;
}

function unblockInput() {
    isActionFlow = false;
    byId("messageInput").disabled = false;
    byId("inputBlocker").classList.remove("active");
    byId("voiceBtn").disabled = false;
    byId("messageInput").focus();
}

window.cancelFlow = function () {
    pendingActionContext = null;
    unblockInput();
    const cancelMsg = makeMessage("bot-message", "Action cancelled. How else can I help?");
    cancelMsg.style.color = "#667085";
    cancelMsg.style.fontStyle = "italic";
    appendMessage(cancelMsg);
};

window.startVoice = function () {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
        alert("Voice input is not supported in this browser.");
        return;
    }

    const btn = byId("voiceBtn");
    const recognition = new SpeechRecognition();
    recognition.lang = "en-IN";
    recognition.interimResults = false;
    recognition.maxAlternatives = 1;
    recognition.start();
    if (btn) btn.classList.add("recording");        // start -> red pulse

    recognition.onresult = (event) => {
        byId("messageInput").value = event.results[0][0].transcript;
        sendMessage();
    };
    recognition.onerror = (event) => console.log(event.error);
    recognition.onend = () => { if (btn) btn.classList.remove("recording"); };  // stop -> back to blue
};

window.handleSendBtn = function () {
    if (isStreaming) stopStream();
    else sendMessage();
};

function setStreamingState(streaming) {
    isStreaming = streaming;
    const button = byId("sendBtn");
    button.textContent = streaming ? "Stop" : "Send";
    button.classList.toggle("streaming", streaming);
    button.title = streaming ? "Stop" : "Send";
    button.setAttribute("aria-label", streaming ? "Stop response" : "Send message");
}

function stopStream() {
    if (currentReader) {
        currentReader.cancel();
        currentReader = null;
    }
    setStreamingState(false);
}

function renderMarkdown(text) {
    if (!text) return "";
    let output = escapeHtml(text);
    output = output.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    output = output.replace(/^(?:\u2022|-)\s+(.+)$/gm, "<li>$1</li>");
    output = output.replace(/^(\d+)\.\s+(.+)$/gm, "<li><strong>$1.</strong> $2</li>");
    output = output.replace(/(<li>.*<\/li>)+/gs, "<ul>$&</ul>");
    output = output.replace(/\n/g, "<br>");
    return output;
}

// Cosmetic typewriter: paint text progressively (word-by-word) for a "typing"
// feel on instant/structured responses. Respects Stop via getStopped().
async function typewriterRender(botDiv, text, getStopped) {
    const words = text.split(/(\s+)/); // keep whitespace as tokens
    let shown = "";
    for (let i = 0; i < words.length; i++) {
        if (getStopped && getStopped()) {
            botDiv.innerHTML = renderMarkdown(text);
            scrollToBottom();
            return;
        }
        shown += words[i];
        botDiv.innerHTML = renderMarkdown(shown);
        scrollToBottom();
        if (words[i].trim()) {
            await new Promise((r) => setTimeout(r, 18));
        }
    }
}

function getStatusMessages(message) {
    const msg = message.toLowerCase();
    if (msg.includes("apply")) return STATUS_MESSAGES.apply;
    if (msg.includes("approve")) return STATUS_MESSAGES.approve;
    if (msg.includes("reject")) return STATUS_MESSAGES.reject;
    if (msg.includes("cancel")) return STATUS_MESSAGES.cancel;
    if (msg.includes("employee") || msg.includes("staff")) return STATUS_MESSAGES.employee;
    if (msg.includes("balance") || msg.includes("leave balance")) return STATUS_MESSAGES.leave;
    if (msg.includes("leave") || msg.includes("history")) return STATUS_MESSAGES.leave_history;
    return STATUS_MESSAGES.default;
}

function setThinking(el, text = "Processing...") {
    el.innerHTML = `<div class="thinking-wrapper"><span class="thinking-dot"></span><span class="thinking-text">${escapeHtml(text)}</span></div>`;
}

function renderTypePicker(data) {
    const wrapper = makeMessage("bot-message action-card");
    const title = document.createElement("div");
    title.className = "action-title";
    title.textContent = data.message;
    wrapper.appendChild(title);

    const options = document.createElement("div");
    options.className = "type-options";
    (data.options || []).forEach((option) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "type-btn";
        button.textContent = option;
        button.addEventListener("click", () => selectLeaveType(option, button));
        options.appendChild(button);
    });
    wrapper.appendChild(options);
    appendMessage(wrapper);
    pendingActionContext = data.context;
    blockInput();
}

function renderDatePicker(data) {
    const wrapper = makeMessage("bot-message action-card");
    const today = new Date().toISOString().split("T")[0];
    const pickerId = "dp_" + Date.now();
    const df = data.default_from || "";
    const dt = data.default_to || df;

    wrapper.innerHTML = `
        <div class="action-title">${escapeHtml(data.message)}</div>
        <div class="date-picker-row">
            <div class="date-field">
                <label for="fromDate_${pickerId}">Start date</label>
                <input type="date" id="fromDate_${pickerId}" min="${today}" value="${df}">
            </div>
            <div class="date-field">
                <label for="toDate_${pickerId}">End date</label>
                <input type="date" id="toDate_${pickerId}" min="${today}" value="${dt}">
            </div>
        </div>
        <div class="halfday-row">
            <div class="halfday-toggle">
                <input type="checkbox" id="halfDay_${pickerId}" onchange="toggleHalfDay('${pickerId}')">
                <label for="halfDay_${pickerId}">Apply as half day</label>
            </div>
            <div id="halfDayOptions_${pickerId}" class="halfday-options">
                <div class="halfday-group">
                    <div class="halfday-label">Beginning From</div>
                    <div class="halfday-buttons">
                        <button type="button" class="half-option-btn selected" id="startFull_${pickerId}" onclick="selectStartHalf('${pickerId}', this)">Full Day</button>
                        <button type="button" class="half-option-btn" id="startSecond_${pickerId}" onclick="selectStartHalf('${pickerId}', this)">Half Day</button>
                    </div>
                </div>
                <div class="halfday-group">
                    <div class="halfday-label">Ending On</div>
                    <div class="halfday-buttons">
                        <button type="button" class="half-option-btn selected" id="endFull_${pickerId}" onclick="selectEndHalf('${pickerId}', this)">Full Day</button>
                        <button type="button" class="half-option-btn" id="endFirst_${pickerId}" onclick="selectEndHalf('${pickerId}', this)">Half Day</button>
                    </div>
                </div>
            </div>
        </div>
        <div id="dateErr_${pickerId}" class="date-error">End date cannot be before start date.</div>
        ${attachmentRowHTML(pickerId)}
        <button type="button" class="confirm-btn" onclick="confirmDates('${pickerId}')">Confirm dates</button>
    `;

    appendMessage(wrapper);
    pendingActionContext = data.context;
    blockInput();
}

window.toggleHalfDay = function (pickerId) {
    const enabled = byId("halfDay_" + pickerId)?.checked;
    byId("halfDayOptions_" + pickerId)?.classList.toggle("active", Boolean(enabled));
};

window.selectStartHalf = function (pickerId, button) {
    byId("startFull_" + pickerId)?.classList.remove("selected");
    byId("startSecond_" + pickerId)?.classList.remove("selected");
    button.classList.add("selected");
};

window.selectEndHalf = function (pickerId, button) {
    byId("endFull_" + pickerId)?.classList.remove("selected");
    byId("endFirst_" + pickerId)?.classList.remove("selected");
    button.classList.add("selected");
};

function renderReasonPicker(data) {
    const wrapper = makeMessage("bot-message action-card");
    const rid = "reasonInput_" + Date.now();
    // If a file was already chosen at the date-picker step, don't show the
    // upload UI again — just show a small "already attached" confirmation.
    const alreadyAttached = !!(pendingAttachment && pendingAttachment.data);
    const attachSection = alreadyAttached
        ? `<div class="attach-row" style="margin-top:14px;display:flex;align-items:center;gap:9px;
                  padding:10px 12px;border:1.5px solid #86efac;border-radius:12px;background:#f0fdf4;">
               <span style="flex:none;width:30px;height:30px;border-radius:8px;background:#dcfce7;
                     display:flex;align-items:center;justify-content:center;font-size:14px;">✓</span>
               <span style="display:flex;flex-direction:column;min-width:0;">
                   <span style="font-size:13px;font-weight:600;color:#047857;white-space:nowrap;
                         overflow:hidden;text-overflow:ellipsis;">${escapeHtml(pendingAttachment.filename)}</span>
                   <span style="font-size:11px;color:#65a30d;">Attachment added</span>
               </span>
           </div>`
        : attachmentRowHTML(rid);
    wrapper.innerHTML = `
        <div class="action-title">${escapeHtml(data.message)}</div>
        <div class="reason-row">
            <textarea id="${rid}" placeholder="Enter reason (optional)..." rows="3"></textarea>
        </div>
        ${attachSection}
        <button type="button" class="confirm-btn" onclick="confirmReason('${rid}')">Submit</button>
    `;
    appendMessage(wrapper);
    pendingActionContext = data.context;
    blockInput();
}

window.handleLeaveAttachment = function (rid, input) {
    const statusEl = byId(rid + "_fstatus");
    const nameEl = byId(rid + "_fname");
    const iconEl = byId(rid + "_ficon");
    const dropEl = byId(rid + "_drop");
    const setStatus = (msg, ok) => {
        if (statusEl) {
            statusEl.textContent = msg;
            statusEl.style.color = ok ? "#059669" : "#dc2626";
        }
    };
    const resetLook = () => {
        if (nameEl) { nameEl.textContent = "Choose a file"; nameEl.style.color = "#1e293b"; }
        if (iconEl) { iconEl.textContent = "⬆"; iconEl.style.background = "#eff6ff"; }
        if (dropEl) { dropEl.style.borderColor = "#cbd5e1"; dropEl.style.background = "#f8fafc"; }
    };
    pendingAttachment = null;
    resetLook();

    const file = input.files && input.files[0];
    if (!file) { setStatus("", true); return; }

    const okTypes = ["image/png", "image/jpeg", "application/pdf"];
    const okExt = /\.(png|jpe?g|pdf)$/i.test(file.name);
    if (!okTypes.includes(file.type) && !okExt) {
        input.value = "";
        return setStatus("Only PNG, JPG or PDF files are allowed.", false);
    }
    if (file.size > 5 * 1024 * 1024) {
        input.value = "";
        return setStatus("File is too large — the limit is 5 MB.", false);
    }

    const reader = new FileReader();
    reader.onload = () => {
        // strip the "data:...;base64," prefix — backend/CRM wants raw base64
        const b64 = String(reader.result).split(",")[1] || "";
        const mime = file.type || (/\.pdf$/i.test(file.name) ? "application/pdf"
                     : /\.png$/i.test(file.name) ? "image/png" : "image/jpeg");
        // persist across picker steps (date picker -> reason picker -> apply)
        pendingAttachment = { filename: file.name, mimetype: mime, data: b64 };
        if (nameEl) { nameEl.textContent = file.name; nameEl.style.color = "#047857"; }
        if (iconEl) { iconEl.textContent = "✓"; iconEl.style.background = "#dcfce7"; }
        if (dropEl) { dropEl.style.borderColor = "#86efac"; dropEl.style.background = "#f0fdf4"; }
        setStatus("Attached · " + _prettySize(file.size), true);
    };
    reader.onerror = () => setStatus("Couldn't read that file — please try again.", false);
    reader.readAsDataURL(file);
};

function _prettySize(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

function renderLeavePicker(data) {
    const wrapper = makeMessage("bot-message action-card");
    const title = document.createElement("div");
    title.className = "action-title";
    title.textContent = data.message;
    wrapper.appendChild(title);

    const list = document.createElement("div");
    list.className = "leave-options";
    wrapper.appendChild(list);

    const all = data.leaves || [];
    const pageSize = data.page_size || 4;
    let shown = 0;

    const moreWrap = document.createElement("div");
    moreWrap.style.marginTop = "8px";

    function renderNext() {
        const slice = all.slice(shown, shown + pageSize);
        slice.forEach((leave) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "leave-option-btn";
            button.textContent = leave.label;
            button.addEventListener("click", () => selectLeaveForAction(leave.leave_guid, data.action, button));
            list.appendChild(button);
        });
        shown += slice.length;
        const left = all.length - shown;
        moreWrap.innerHTML = "";
        if (left > 0) {
            const more = document.createElement("button");
            more.type = "button";
            more.className = "leave-option-btn";
            more.style.opacity = ".75";
            more.textContent = "Show more (" + left + " more)";
            more.addEventListener("click", renderNext);
            moreWrap.appendChild(more);
        }
    }

    renderNext();
    wrapper.appendChild(moreWrap);
    appendMessage(wrapper);
    pendingActionContext = { action: data.action, ...(data.context || {}) };
    blockInput();
}

window.selectLeaveType = function (option, button) {
    const leaveTypeName = option.split(" (")[0].trim();
    document.querySelectorAll(".type-btn").forEach((btn) => btn.classList.remove("selected"));
    button.classList.add("selected");

    if (!pendingActionContext) return;

    pendingActionContext.leave_type_name = leaveTypeName;
    if (pendingActionContext.next_step === "date_picker") {
        const snapshot = { ...pendingActionContext, leave_type_name: leaveTypeName };
        setTimeout(() => {
            renderDatePicker({
                message: `Select dates for ${snapshot.employee_display_name}'s ${leaveTypeName} leave:`,
                context: snapshot
            });
        }, 160);
    } else {
        sendActionWithContext(pendingActionContext);
    }
};

window.confirmDates = function (pickerId) {
    const from = byId("fromDate_" + pickerId)?.value;
    const to = byId("toDate_" + pickerId)?.value;
    const isHalf = byId("halfDay_" + pickerId)?.checked;
    const errEl = byId("dateErr_" + pickerId);

    const showError = (message) => {
        errEl.textContent = message;
        errEl.style.display = "block";
    };

    if (!from) return showError("Please select a start date.");
    if (!to) return showError("Please select an end date.");
    if (to < from) return showError("End date cannot be before start date.");

    if (!pendingActionContext) return;

    pendingActionContext.from_date = from;
    pendingActionContext.to_date = to;

    // NOTE: this is a rough preview only. The backend recomputes the real
    // chargeable days (it excludes weekends + public holidays).
    let totalDays = countWorkingDaysPreview(from, to);

    if (isHalf) {
        const startSecond = byId("startSecond_" + pickerId)?.classList.contains("selected");
        const endFirst = byId("endFirst_" + pickerId)?.classList.contains("selected");

        if (startSecond && endFirst) {
            return showError("A half-day start and a half-day end can't be one leave — there's no continuous full period. Apply separate leaves for each half day.");
        }

        if (startSecond) totalDays -= 0.5;
        if (endFirst) totalDays -= 0.5;

        const halfInfo = [];
        if (startSecond) halfInfo.push("start:second");
        if (endFirst) halfInfo.push("end:first");
        pendingActionContext.half_day_info = halfInfo.join(",");
        // send the flags EXPLICITLY too, so there's no marker-translation
        // ambiguity: Beginning From = Half -> beginning_from="half", etc.
        pendingActionContext.beginning_from = startSecond ? "half" : "full";
        pendingActionContext.ending_in = endFirst ? "half" : "full";
    } else {
        pendingActionContext.half_day_info = null;
        pendingActionContext.beginning_from = "full";
        pendingActionContext.ending_in = "full";
    }

    pendingActionContext.no_of_days = Math.max(totalDays, 0.5);
    sendActionWithContext(pendingActionContext);
};

// Preview-only working-day count (excludes Sat/Sun). Holidays are applied by
// the backend, so the confirmed total may be a little lower than this preview.
function countWorkingDaysPreview(from, to) {
    const start = new Date(from);
    const end = new Date(to);
    let count = 0;
    const cur = new Date(start);
    while (cur <= end) {
        const d = cur.getDay();
        if (d !== 0 && d !== 6) count++;
        cur.setDate(cur.getDate() + 1);
    }
    return count || 0.5;
}

window.confirmReason = function (rid) {
    const field = rid ? byId(rid) : byId("reasonInput");
    const reason = field?.value?.trim() || "";

    // Reason is optional — an empty reason is allowed (applied as N/A).
    if (pendingActionContext) {
        pendingActionContext.reason = reason;
        pendingActionContext.reason_asked = true;
        sendActionWithContext(pendingActionContext);
    }
};

window.selectLeaveForAction = async function (leaveGuid, action, button) {
    if (button.disabled) return;
    document.querySelectorAll(".leave-option-btn").forEach((btn) => {
        btn.disabled = true;
        btn.classList.remove("selected");
    });
    button.classList.add("selected");

    const botDiv = appendMessage(makeMessage("bot-message"));
    setThinking(botDiv);

    try {
        const response = await fetch(CHAT_ENDPOINT, {
            method: "POST",
            headers: { "Content-Type": "application/json", "Authorization": "Bearer " + hrToken },
            body: JSON.stringify({ message: action + " " + leaveGuid })
        });
        if (isAuthError(response)) {
            pendingActionContext = null;
            unblockInput();
            botDiv.remove();
            setUnauthorized();
            return;
        }
        const data = await response.json();
        pendingActionContext = null;
        unblockInput();
        renderBotResponse(botDiv, data);
    } catch (err) {
        pendingActionContext = null;
        unblockInput();
        botDiv.textContent = "Something went wrong. Please try again.";
    }
    scrollToBottom();
};

async function sendActionWithContext(context) {
    // carry any file the user chose (at the date or reason picker) into apply
    if (pendingAttachment && pendingAttachment.data) {
        context.attachment = pendingAttachment;
    }
    const leaveType = context.leave_type_name || "leave";
    const fromDate = context.from_date || "";
    const toDate = context.to_date || "";
    const reason = context.reason || "";
    const daysInfo = context.no_of_days ? ` | ${context.no_of_days} day${context.no_of_days === 1 ? "" : "s"}` : "";
    const dateInfo = fromDate ? " | " + fromDate + (toDate && toDate !== fromDate ? " -> " + toDate : "") : "";

    appendMessage(makeMessage("user-message", leaveType + dateInfo + daysInfo + (reason ? " | " + reason : "")));

    const botDiv = appendMessage(makeMessage("bot-message"));
    setThinking(botDiv);

    const parts = [];
    parts.push(leaveType && leaveType !== "leave" ? "apply " + leaveType : "apply leave");
    if (fromDate && toDate) parts.push("from " + fromDate + " to " + toDate);
    if (reason) parts.push("reason " + reason);

    try {
        const response = await fetch(CHAT_ENDPOINT, {
            method: "POST",
            headers: { "Content-Type": "application/json", "Authorization": "Bearer " + hrToken },
            body: JSON.stringify({
                message: parts.join(" "),
                context
            })
        });
        if (isAuthError(response)) {
            pendingActionContext = null;
            unblockInput();
            botDiv.remove();
            setUnauthorized();
            return;
        }
        const data = await response.json();
        pendingActionContext = null;
        unblockInput();
        renderBotResponse(botDiv, data);
    } catch (err) {
        pendingActionContext = null;
        unblockInput();
        botDiv.textContent = "Something went wrong. Please try again.";
    }
    scrollToBottom();
}

window.downloadAttachment = function (filename, mimetype, b64) {
    try {
        const bin = atob(b64);
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        const blob = new Blob([bytes], { type: mimetype || "application/octet-stream" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = filename || "attachment";
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1500);
    } catch (e) {
        console.error("download failed", e);
    }
};

function attachmentButton(att) {
    // returns a styled "download" button element, or null
    if (!att || !att.documentbody) return null;
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = "⬇ " + (att.filename || "Download");
    b.title = "Download attachment";
    b.style.cssText =
        "display:inline-flex;align-items:center;gap:6px;margin-top:8px;" +
        "padding:6px 12px;border-radius:8px;border:1px solid rgba(37,99,235,0.35);" +
        "background:rgba(37,99,235,0.08);color:#2563eb;font-size:13px;" +
        "font-weight:600;cursor:pointer;";
    b.addEventListener("click", () =>
        window.downloadAttachment(att.filename, att.mimetype, att.documentbody));
    return b;
}

function renderConfirm(data) {
    const wrapper = makeMessage("bot-message action-card");
    wrapper.style.display = "block";

    const title = document.createElement("div");
    title.className = "action-title";
    title.textContent = data.message;
    wrapper.appendChild(title);

    if (data.detail) {
        const card = document.createElement("div");
        card.style.cssText =
            "margin-top:10px;padding:12px 14px;border:1px solid rgba(16,24,40,0.1);" +
            "border-radius:12px;background:linear-gradient(180deg,rgba(37,99,235,0.05),rgba(16,24,40,0.02));";

        const row = document.createElement("div");
        row.style.cssText = "display:flex;align-items:center;gap:10px;flex-wrap:wrap;";

        const lt = document.createElement("span");
        lt.textContent = data.detail.leave_type || "Leave";
        lt.style.cssText = "font-weight:700;font-size:15px;";

        const when = document.createElement("span");
        when.textContent = "📅 " + (data.detail.when || "");
        when.style.cssText = "opacity:0.85;font-size:13px;";

        const days = document.createElement("span");
        days.textContent = data.detail.days || "";
        days.style.cssText =
            "font-size:12px;padding:3px 10px;border-radius:999px;" +
            "background:rgba(37,99,235,0.14);color:#2563eb;font-weight:700;";

        row.appendChild(lt);
        row.appendChild(when);
        if (data.detail.days) row.appendChild(days);
        card.appendChild(row);

        const dl = attachmentButton(data.detail.attachment);
        if (dl) card.appendChild(dl);

        wrapper.appendChild(card);
    }

    const row = document.createElement("div");
    row.style.display = "flex";
    row.style.gap = "8px";
    row.style.marginTop = "12px";

    const yes = document.createElement("button");
    yes.type = "button";
    yes.className = "confirm-btn";
    yes.textContent = "Yes, proceed";
    yes.addEventListener("click", () => answerConfirm("yes"));

    const no = document.createElement("button");
    no.type = "button";
    no.className = "confirm-btn";
    no.style.background = "transparent";
    no.style.border = "0.5px solid rgba(16,24,40,0.25)";
    no.style.color = "inherit";
    no.textContent = "No";
    no.addEventListener("click", () => answerConfirm("no"));

    row.appendChild(yes);
    row.appendChild(no);
    wrapper.appendChild(row);
    appendMessage(wrapper);

    pendingConfirm = data.context;
}

window.answerConfirm = function (reply) {
    document.querySelectorAll(".action-card .confirm-btn").forEach((b) => (b.disabled = true));
    submitReplyWithConfirm(reply);
};

async function submitReplyWithConfirm(reply) {
    const ctx = pendingConfirm;
    pendingConfirm = null;
    appendMessage(makeMessage("user-message", reply));
    const botDiv = appendMessage(makeMessage("bot-message"));
    setThinking(botDiv);
    try {
        const response = await fetch(CHAT_ENDPOINT, {
            method: "POST",
            headers: { "Content-Type": "application/json", "Authorization": "Bearer " + hrToken },
            body: JSON.stringify({ message: reply, context: ctx })
        });
        if (isAuthError(response)) { botDiv.remove(); setUnauthorized(); return; }
        const ct = response.headers.get("content-type") || "";
        if (ct.includes("application/json")) {
            renderBotResponse(botDiv, await response.json());
        } else {
            const txt = (await response.text()).replace(/\x1fLIVE\x1f/g, "");
            botDiv.innerHTML = renderMarkdown(txt);
        }
    } catch (err) {
        botDiv.textContent = "Something went wrong. Please try again.";
    }
    scrollToBottom();
}

function balanceGrid(items) {
    const grid = document.createElement("div");
    grid.style.display = "grid";
    grid.style.gridTemplateColumns = "repeat(auto-fit, minmax(120px, 1fr))";
    grid.style.gap = "10px";
    (items || []).forEach((it) => {
        const bal = (it.balance === null || it.balance === undefined) ? null : Number(it.balance);
        const c = document.createElement("div");
        c.style.border = "1px solid rgba(16,24,40,0.10)";
        c.style.borderRadius = "12px";
        c.style.padding = "12px 14px";
        c.style.background = "rgba(127,127,127,0.04)";

        const type = document.createElement("div");
        type.style.fontSize = "12px";
        type.style.fontWeight = "600";
        type.style.opacity = ".6";
        type.style.marginBottom = "4px";
        type.textContent = it.type || "Leave";

        const num = document.createElement("div");
        num.style.fontSize = "22px";
        num.style.fontWeight = "700";
        num.style.lineHeight = "1";
        num.style.color = "inherit";
        const shown = (bal === null) ? "—" : (Number.isInteger(bal) ? bal : bal.toFixed(1));
        const lowTag = (bal !== null && bal <= 2)
            ? ' <span style="font-size:11px;font-weight:600;opacity:.45">· low</span>' : "";
        num.innerHTML = shown +
            ' <span style="font-size:12px;font-weight:600;opacity:.6">days</span>' + lowTag;

        c.appendChild(type);
        c.appendChild(num);
        grid.appendChild(c);
    });
    return grid;
}

function downloadBlob(filename, content, mime) {
    const blob = new Blob([content], { type: mime || "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function svgToPng(svgEl, filename) {
    const clone = svgEl.cloneNode(true);
    const w = parseInt(svgEl.getAttribute("width")) || svgEl.clientWidth || 480;
    const h = parseInt(svgEl.getAttribute("height")) || svgEl.clientHeight || 300;
    const xml = new XMLSerializer().serializeToString(clone);
    const svg64 = "data:image/svg+xml;base64," + btoa(unescape(encodeURIComponent(xml)));
    const img = new Image();
    img.onload = () => {
        const scale = 2;
        const canvas = document.createElement("canvas");
        canvas.width = w * scale;
        canvas.height = h * scale;
        const ctx = canvas.getContext("2d");
        ctx.fillStyle = "#ffffff";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.scale(scale, scale);
        ctx.drawImage(img, 0, 0);
        const a = document.createElement("a");
        a.href = canvas.toDataURL("image/png");
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
    };
    img.src = svg64;
}

function comparisonCSV(data) {
    const hasCount = (data.items || []).some((i) => i.count !== undefined);
    const rows = [["Name", data.metric || "Value"].concat(hasCount ? ["Records"] : [])];
    (data.items || []).forEach((it) => {
        const v = it.value === null || it.value === undefined ? (it.note || "-") : it.value;
        rows.push([it.name, v].concat(hasCount ? [it.count != null ? it.count : ""] : []));
    });
    return rows.map((r) => r.map((c) => '"' + String(c).replace(/"/g, '""') + '"').join(",")).join("\n");
}

function renderComparison(data) {
    const items = (data.items || []);
    const wrapper = makeMessage("bot-message");
    wrapper.style.display = "block";

    // bounded card so nothing overflows the chat bubble
    const card = document.createElement("div");
    card.style.border = "1px solid rgba(16,24,40,0.10)";
    card.style.borderRadius = "12px";
    card.style.padding = "14px 16px";
    card.style.background = "rgba(127,127,127,0.04)";
    card.style.maxWidth = "100%";
    card.style.overflow = "hidden";
    card.style.boxSizing = "border-box";

    const head = document.createElement("div");
    head.style.fontWeight = "600";
    head.textContent = data.title || "Comparison";
    card.appendChild(head);

    const sub = document.createElement("div");
    sub.style.fontSize = "12px";
    sub.style.opacity = ".6";
    sub.style.margin = "2px 0 12px";
    sub.textContent = data.metric + (data.period ? "  ·  " + data.period : "");
    card.appendChild(sub);

    // --- responsive SVG bar chart (name on its own line; no clipping) ---
    const valued = items.filter((i) => i.value !== null && i.value !== undefined).map((i) => Number(i.value));
    const maxVal = Math.max(1, ...valued);
    const hiVal = valued.length ? Math.max(...valued) : null;
    const loVal = valued.length ? Math.min(...valued) : null;

    const VBW = 480;
    const nameH = 17, barH = 20, gap = 16, padT = 4, valueW = 60;
    const barMaxW = VBW - 4 - valueW;       // leave room for the value text
    const rowH = nameH + barH;
    const H = padT + items.length * (rowH + gap);
    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", "0 0 " + VBW + " " + H);
    svg.setAttribute("width", "100%");
    svg.setAttribute("height", H);
    svg.setAttribute("preserveAspectRatio", "xMinYMin meet");
    svg.style.display = "block";
    svg.style.maxWidth = "100%";

    const mkText = (x, y, t, opts) => {
        opts = opts || {};
        const el = document.createElementNS(svgNS, "text");
        el.setAttribute("x", x); el.setAttribute("y", y);
        el.setAttribute("font-size", opts.size || "12.5");
        el.setAttribute("font-weight", opts.weight || "600");
        el.setAttribute("fill", opts.fill || "#101828");
        if (opts.anchor) el.setAttribute("text-anchor", opts.anchor);
        if (opts.opacity) el.setAttribute("opacity", opts.opacity);
        el.textContent = t;
        return el;
    };

    items.forEach((it, idx) => {
        const top = padT + idx * (rowH + gap);
        const isNull = it.value === null || it.value === undefined;
        const val = Number(it.value) || 0;
        const bw = isNull ? 0 : Math.max(3, (val / maxVal) * barMaxW);

        // name line (full width, no clipping) + most/least tag
        let tag = "";
        if (!isNull && hiVal !== loVal && val === hiVal) tag = "  ·  most";
        else if (!isNull && hiVal !== loVal && val === loVal) tag = "  ·  least";
        const nameEl = mkText(2, top + 12, it.name + tag, { size: "12.5", weight: "600" });
        const titleEl = document.createElementNS(svgNS, "title");
        titleEl.textContent = it.name;
        nameEl.appendChild(titleEl);
        svg.appendChild(nameEl);

        const by = top + nameH;
        const track = document.createElementNS(svgNS, "rect");
        track.setAttribute("x", 2); track.setAttribute("y", by);
        track.setAttribute("width", barMaxW); track.setAttribute("height", barH);
        track.setAttribute("rx", "6"); track.setAttribute("fill", "rgba(16,24,40,0.06)");
        svg.appendChild(track);

        if (!isNull) {
            const bar = document.createElementNS(svgNS, "rect");
            bar.setAttribute("x", 2); bar.setAttribute("y", by);
            bar.setAttribute("width", bw); bar.setAttribute("height", barH);
            bar.setAttribute("rx", "6");
            bar.setAttribute("fill", (val === hiVal && hiVal !== loVal)
                ? "rgba(16,24,40,0.78)" : "rgba(16,24,40,0.42)");
            svg.appendChild(bar);
        }

        const vtext = isNull ? (it.note || "—") : (it.value + " " + (data.unit || ""));
        svg.appendChild(mkText(2 + (isNull ? 0 : bw) + 8, by + barH / 2 + 4, vtext,
            { size: "11.5", weight: "700" }));
    });
    card.appendChild(svg);

    if (data.summary) {
        const sm = document.createElement("div");
        sm.style.fontSize = "13px";
        sm.style.marginTop = "10px";
        sm.textContent = data.summary;
        card.appendChild(sm);
    }

    // --- export buttons (inside the card) ---
    const bar = document.createElement("div");
    bar.style.display = "flex";
    bar.style.flexWrap = "wrap";
    bar.style.gap = "8px";
    bar.style.marginTop = "12px";

    const ICON_XLS = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;margin-right:5px"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M9 3v18M15 3v18"/></svg>';
    const ICON_IMG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;margin-right:5px"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>';

    const mkBtn = (html, fn) => {
        const b = document.createElement("button");
        b.type = "button";
        b.style.cssText = "display:inline-flex;align-items:center;font-size:12.5px;font-weight:600;padding:7px 12px;border:0.5px solid rgba(16,24,40,0.25);background:transparent;color:inherit;border-radius:8px;cursor:pointer;";
        b.innerHTML = html;
        b.addEventListener("click", fn);
        return b;
    };
    bar.appendChild(mkBtn(ICON_XLS + "Excel (CSV)", () =>
        downloadBlob("comparison.csv", comparisonCSV(data), "text/csv")));
    bar.appendChild(mkBtn(ICON_IMG + "Image (PNG)", () => svgToPng(svg, "comparison.png")));
    card.appendChild(bar);

    wrapper.appendChild(card);
    appendMessage(wrapper);
    scrollToBottom();
}

function renderBalanceGroup(data) {
    const wrapper = makeMessage("bot-message");
    wrapper.style.display = "block";

    const head = document.createElement("div");
    head.style.fontWeight = "500";
    head.style.marginBottom = "12px";
    head.textContent = data.intro || "Leave balances";
    wrapper.appendChild(head);

    (data.groups || []).forEach((g, idx) => {
        const name = document.createElement("div");
        name.style.fontWeight = "700";
        name.style.fontSize = "13px";
        name.style.margin = (idx ? "14px" : "0") + "0 8px 0";
        name.style.marginTop = idx ? "14px" : "0";
        name.style.marginBottom = "8px";
        name.textContent = g.name;
        wrapper.appendChild(name);

        if (g.denied) {
            const d = document.createElement("div");
            d.style.opacity = ".6";
            d.style.fontSize = "13px";
            d.textContent = "Not authorized to view this person's balance.";
            wrapper.appendChild(d);
        } else if (!g.items || !g.items.length) {
            const d = document.createElement("div");
            d.style.opacity = ".6";
            d.style.fontSize = "13px";
            d.textContent = "No balance records found.";
            wrapper.appendChild(d);
        } else {
            wrapper.appendChild(balanceGrid(g.items));
        }
    });

    appendMessage(wrapper);
    scrollToBottom();
}

function renderBalance(data) {
    const wrapper = makeMessage("bot-message");
    wrapper.style.display = "block";

    const head = document.createElement("div");
    head.style.fontWeight = "500";
    head.style.marginBottom = "10px";
    head.textContent = data.intro || "Leave balances";
    wrapper.appendChild(head);

    wrapper.appendChild(balanceGrid(data.items || []));
    appendMessage(wrapper);
    scrollToBottom();
}

function renderProfile(data) {
    const wrapper = makeMessage("bot-message");
    wrapper.style.display = "block";

    if (data.intro) {
        const head = document.createElement("div");
        head.style.fontWeight = "500";
        head.style.marginBottom = "10px";
        head.textContent = data.intro;
        wrapper.appendChild(head);
    }

    const card = document.createElement("div");
    card.style.border = "1px solid rgba(16,24,40,0.10)";
    card.style.borderRadius = "12px";
    card.style.padding = "14px";
    card.style.background = "rgba(255,255,255,0.65)";

    // header: avatar (initials) + name
    const header = document.createElement("div");
    header.style.display = "flex";
    header.style.alignItems = "center";
    header.style.gap = "12px";
    header.style.marginBottom = "12px";

    const name = (data.name || "").trim();
    const pretty = name === name.toUpperCase()
        ? name.toLowerCase().replace(/\b\w/g, (m) => m.toUpperCase())
        : name;
    const initials = pretty.split(/\s+/).map((w) => w[0] || "").slice(0, 2).join("").toUpperCase();

    const avatar = document.createElement("div");
    avatar.style.width = "42px";
    avatar.style.height = "42px";
    avatar.style.borderRadius = "50%";
    avatar.style.flex = "0 0 auto";
    avatar.style.display = "flex";
    avatar.style.alignItems = "center";
    avatar.style.justifyContent = "center";
    avatar.style.fontWeight = "700";
    avatar.style.fontSize = "15px";
    avatar.style.color = "inherit";
    avatar.style.opacity = ".8";
    avatar.style.background = "rgba(16,24,40,0.06)";
    avatar.textContent = initials || "👤";

    const nameEl = document.createElement("div");
    nameEl.style.fontWeight = "700";
    nameEl.style.fontSize = "15px";
    nameEl.textContent = pretty;

    header.appendChild(avatar);
    header.appendChild(nameEl);
    card.appendChild(header);

    // labelled fields grid
    const grid = document.createElement("div");
    grid.style.display = "grid";
    grid.style.gridTemplateColumns = "repeat(auto-fit, minmax(150px, 1fr))";
    grid.style.gap = "10px 18px";
    (data.fields || []).forEach((f) => {
        const cell = document.createElement("div");
        const label = document.createElement("div");
        label.style.fontSize = "11px";
        label.style.opacity = ".55";
        label.style.textTransform = "uppercase";
        label.style.letterSpacing = ".03em";
        label.textContent = f[0];
        const val = document.createElement("div");
        val.style.fontSize = "14px";
        val.style.fontWeight = "500";
        val.textContent = f[1];
        cell.appendChild(label);
        cell.appendChild(val);
        grid.appendChild(cell);
    });
    card.appendChild(grid);

    wrapper.appendChild(card);
    appendMessage(wrapper);
    scrollToBottom();
}

function renderList(data) {
    const items = data.items || [];
    const pageSize = data.page_size || 5;
    let shown = 0;

    const STATUS_COLORS = {
        "requested": ["#FEF0C7", "#B54708"],
        "approved":  ["#D1FADF", "#027A48"],
        "rejected":  ["#FEE4E2", "#B42318"],
        "cancelled": ["#EAECF0", "#475467"],
        "canceled":  ["#EAECF0", "#475467"]
    };

    const wrapper = makeMessage("bot-message");
    wrapper.style.display = "block";

    const head = document.createElement("div");
    head.style.fontWeight = "500";
    head.style.marginBottom = "10px";
    head.textContent = data.intro || data.title || (items.length + " records");
    wrapper.appendChild(head);

    const listEl = document.createElement("div");
    listEl.style.display = "flex";
    listEl.style.flexDirection = "column";
    listEl.style.gap = "8px";
    wrapper.appendChild(listEl);

    const moreBtn = document.createElement("button");
    moreBtn.type = "button";
    moreBtn.className = "confirm-btn";
    moreBtn.style.marginTop = "10px";

    function card(item, index) {
        const c = document.createElement("div");
        c.style.border = "1px solid rgba(16,24,40,0.10)";
        c.style.borderRadius = "10px";
        c.style.padding = "10px 12px";
        c.style.background = "rgba(255,255,255,0.65)";

        // header: index + primary  ......  badge
        const header = document.createElement("div");
        header.style.display = "flex";
        header.style.alignItems = "center";
        header.style.justifyContent = "space-between";
        header.style.gap = "8px";

        const titleWrap = document.createElement("div");
        titleWrap.style.fontWeight = "600";
        titleWrap.innerHTML =
            '<span style="opacity:.45;margin-right:6px">' + index + ".</span>" +
            escapeHtml(item.primary || "");
        header.appendChild(titleWrap);

        if (item.badge) {
            const key = String(item.badge).toLowerCase();
            const colors = STATUS_COLORS[key] || ["#EAECF0", "#475467"];
            const badge = document.createElement("span");
            badge.textContent = item.badge;
            badge.style.background = colors[0];
            badge.style.color = colors[1];
            badge.style.fontSize = "12px";
            badge.style.fontWeight = "600";
            badge.style.padding = "2px 10px";
            badge.style.borderRadius = "999px";
            badge.style.whiteSpace = "nowrap";
            header.appendChild(badge);
        }
        c.appendChild(header);

        // labelled fields
        if (item.fields && item.fields.length) {
            const grid = document.createElement("div");
            grid.style.display = "flex";
            grid.style.flexWrap = "wrap";
            grid.style.gap = "4px 18px";
            grid.style.marginTop = "6px";
            item.fields.forEach((f) => {
                const cell = document.createElement("div");
                cell.style.fontSize = "13px";
                cell.innerHTML =
                    '<span style="opacity:.55">' + escapeHtml(f[0]) + ":</span> " +
                    '<span>' + escapeHtml(f[1]) + "</span>";
                grid.appendChild(cell);
            });
            c.appendChild(grid);
        }
        if (item.attachment && item.attachment.documentbody) {
            const dl = attachmentButton(item.attachment);
            if (dl) { dl.style.marginTop = "8px"; c.appendChild(dl); }
        }
        return c;
    }

    function renderMore() {
        const next = items.slice(shown, shown + pageSize);
        next.forEach((it, i) => listEl.appendChild(card(it, shown + i + 1)));
        shown += next.length;
        const left = items.length - shown;
        if (left > 0) {
            moreBtn.textContent = "Show more (" + left + " more)";
        } else {
            moreBtn.remove();
        }
        scrollToBottom();
    }

    moreBtn.addEventListener("click", renderMore);
    wrapper.appendChild(moreBtn);

    // --- export (CSV + PNG) for the whole list ---
    const xbar = document.createElement("div");
    xbar.style.display = "flex";
    xbar.style.flexWrap = "wrap";
    xbar.style.gap = "8px";
    xbar.style.marginTop = "12px";
    const ICON_XLS2 = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;margin-right:5px"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M9 3v18M15 3v18"/></svg>';
    const ICON_IMG2 = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;margin-right:5px"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>';
    const xbtn = (html, fn) => {
        const b = document.createElement("button");
        b.type = "button";
        b.style.cssText = "display:inline-flex;align-items:center;font-size:12.5px;font-weight:600;padding:7px 12px;border:0.5px solid rgba(16,24,40,0.25);background:transparent;color:inherit;border-radius:8px;cursor:pointer;";
        b.innerHTML = html;
        b.addEventListener("click", fn);
        return b;
    };
    const fname = (data.kind === "employee" ? "employees" : "leaves");
    xbar.appendChild(xbtn(ICON_XLS2 + "Excel (CSV)", () =>
        downloadBlob(fname + ".csv", listCSV(data), "text/csv")));
    xbar.appendChild(xbtn(ICON_IMG2 + "Image (PNG)", () =>
        tableToPng(data.intro || "List", listColumns(data), fname + ".png")));
    wrapper.appendChild(xbar);

    appendMessage(wrapper);
    renderMore(); // first page
    unblockInput();
}

function listColumns(data) {
    const items = data.items || [];
    if (!items.length) return { header: [], rows: [] };
    const fieldLabels = (items[0].fields || []).map((f) => f[0]);
    const hasBadge = items.some((i) => i.badge);
    const header = ["#", "Name"].concat(hasBadge ? ["Status"] : []).concat(fieldLabels);
    const rows = items.map((it, i) => {
        const fv = (it.fields || []).map((f) => (f[1] == null ? "" : String(f[1])));
        return [String(i + 1), it.primary || ""].concat(hasBadge ? [it.badge || ""] : []).concat(fv);
    });
    return { header: header, rows: rows };
}

function listCSV(data) {
    const c = listColumns(data);
    if (!c.header.length) return "";
    const esc = (v) => '"' + String(v).replace(/"/g, '""') + '"';
    return [c.header].concat(c.rows).map((r) => r.map(esc).join(",")).join("\n");
}

function tableToPng(title, cols, filename) {
    const header = cols.header || [], rows = cols.rows || [];
    if (!header.length) return;
    const cvs = document.createElement("canvas");
    const ctx = cvs.getContext("2d");
    const scale = 2, pad = 14, rowH = 30, headH = 36, titleH = 34, font = "13px sans-serif";
    ctx.font = font;
    const colW = header.map((h, i) => {
        let w = ctx.measureText(String(h)).width;
        rows.forEach((r) => { w = Math.max(w, ctx.measureText(String(r[i] || "")).width); });
        return Math.min(Math.ceil(w) + 22, 240);
    });
    const tableW = colW.reduce((a, b) => a + b, 0);
    const W = tableW + pad * 2;
    const H = titleH + headH + rows.length * rowH + pad * 2;

    cvs.width = W * scale; cvs.height = H * scale;
    ctx.scale(scale, scale);
    ctx.fillStyle = "#ffffff"; ctx.fillRect(0, 0, W, H);
    ctx.textBaseline = "middle";

    ctx.fillStyle = "#101828"; ctx.font = "600 15px sans-serif";
    ctx.fillText(String(title), pad, pad + 12);

    let y = pad + titleH;
    ctx.font = "700 13px sans-serif"; ctx.fillStyle = "#101828";
    let x = pad;
    header.forEach((h, i) => { ctx.fillText(String(h), x + 6, y + headH / 2); x += colW[i]; });
    ctx.strokeStyle = "rgba(16,24,40,0.25)"; ctx.beginPath();
    ctx.moveTo(pad, y + headH); ctx.lineTo(pad + tableW, y + headH); ctx.stroke();
    y += headH;

    ctx.font = "13px sans-serif";
    rows.forEach((r) => {
        x = pad;
        r.forEach((cell, i) => {
            ctx.fillStyle = "#101828";
            let txt = String(cell);
            while (txt && ctx.measureText(txt).width > colW[i] - 12) txt = txt.slice(0, -1);
            if (txt !== String(cell)) txt = txt.slice(0, -1) + "…";
            ctx.fillText(txt, x + 6, y + rowH / 2);
            x += colW[i];
        });
        ctx.strokeStyle = "rgba(16,24,40,0.08)"; ctx.beginPath();
        ctx.moveTo(pad, y + rowH); ctx.lineTo(pad + tableW, y + rowH); ctx.stroke();
        y += rowH;
    });

    const a = document.createElement("a");
    a.href = cvs.toDataURL("image/png");
    a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
}

function renderBotResponse(botDiv, data) {
    if (data && data.type) {
        switch (data.type) {
            case "type_picker":
                botDiv.remove();
                renderTypePicker(data);
                break;
            case "date_picker":
                botDiv.remove();
                renderDatePicker(data);
                break;
            case "reason_picker":
                botDiv.remove();
                renderReasonPicker(data);
                break;
            case "leave_picker":
                botDiv.remove();
                renderLeavePicker(data);
                break;
            case "list":
                botDiv.remove();
                renderList(data);
                break;
            case "balance":
                botDiv.remove();
                renderBalance(data);
                break;
            case "balance_group":
                botDiv.remove();
                renderBalanceGroup(data);
                break;
            case "comparison":
                botDiv.remove();
                renderComparison(data);
                break;
            case "profile":
                botDiv.remove();
                renderProfile(data);
                break;
            case "confirm":
                botDiv.remove();
                renderConfirm(data);
                break;
            case "success":
                botDiv.className = "bot-message success-message";
                botDiv.innerHTML = renderMarkdown(data.message);
                unblockInput();
                break;
            case "error":
                botDiv.className = "bot-message error-message";
                botDiv.innerHTML = renderMarkdown(data.message);
                unblockInput();
                break;
            default:
                botDiv.innerHTML = renderMarkdown(data.message || JSON.stringify(data));
                unblockInput();
        }
    } else {
        botDiv.innerHTML = renderMarkdown(typeof data === "string" ? data : JSON.stringify(data));
        unblockInput();
    }
}

async function sendMessage() {
    if (isActionFlow) return;

    const input = byId("messageInput");
    const message = input.value.trim();
    if (!message) return;
    if (!hrToken) {
        alert("User token missing. Please login again.");
        return;
    }
    pendingAttachment = null;   // fresh conversation turn -> drop any stale file

    appendMessage(makeMessage("user-message", message));
    input.value = "";

    const botDiv = appendMessage(makeMessage("bot-message"));
    const statusMessages = getStatusMessages(message);
    setThinking(botDiv, statusMessages[0]);

    const thinkingText = botDiv.querySelector(".thinking-text");
    let statusIndex = 1;
    let firstChunk = true;
    const statusInterval = setInterval(() => {
        if (firstChunk && thinkingText) {
            thinkingText.textContent = statusMessages[statusIndex % statusMessages.length];
            statusIndex++;
        }
    }, 2000);

    setStreamingState(true);

    // If a confirmation is pending, a typed reply (yes/no/haan/naa/anything)
    // is sent WITH that context so the backend continues the same action.
    const confirmCtx = pendingConfirm;
    pendingConfirm = null;
    const requestBody = confirmCtx
        ? { message, context: confirmCtx }
        : { message };

    try {
        const response = await fetch(CHAT_ENDPOINT, {
            method: "POST",
            headers: { "Content-Type": "application/json", "Authorization": "Bearer " + hrToken },
            body: JSON.stringify(requestBody)
        });

        if (isAuthError(response)) {
            clearInterval(statusInterval);
            setStreamingState(false);
            botDiv.remove();
            setUnauthorized();
            return;
        }

        if (!response.ok) {
            clearInterval(statusInterval);
            setStreamingState(false);
            botDiv.className = "bot-message error-message";
            botDiv.textContent = "Something went wrong. Please try again.";
            return;
        }

        const contentType = response.headers.get("content-type");
        if (contentType && contentType.includes("application/json")) {
            clearInterval(statusInterval);
            setStreamingState(false);
            firstChunk = false;
            renderBotResponse(botDiv, await response.json());
            scrollToBottom();
            return;
        }

        currentReader = response.body.getReader();
        const decoder = new TextDecoder();
        let fullText = "";
        let chunkCount = 0;
        let streamStart = 0;

        while (true) {
            const { done, value } = await currentReader.read();
            if (done) break;

            const chunk = decoder.decode(value, { stream: true });
            const cleanedChunk = chunk.replace(/\x1fLIVE\x1f/g, "");
            if (!cleanedChunk.trim()) continue;
            if (!chunk.trim()) continue;

            if (firstChunk) {
                clearInterval(statusInterval);
                firstChunk = false;

                try {
                    const parsed = JSON.parse(chunk);
                    if (parsed && parsed.type) {
                        setStreamingState(false);
                        currentReader = null;
                        renderBotResponse(botDiv, parsed);
                        scrollToBottom();
                        return;
                    }
                } catch (err) {
                    // Not JSON; continue rendering the stream as text.
                }
            }

            if (!streamStart) streamStart = Date.now();
            chunkCount++;
            fullText += cleanedChunk;
            botDiv.innerHTML = renderMarkdown(fullText);
            scrollToBottom();
        }

        currentReader = null;

        // Was this a genuine live token stream (Ollama free-text — many chunks
        // spread over time)? If so it already "typed" live, so DON'T re-type it.
        // A structured response arrives as a quick burst (1-2 chunks, ~instant)
        // -> add the cosmetic typewriter so it looks like typing.
        const spread = streamStart ? (Date.now() - streamStart) : 0;
        const wasLiveStream = chunkCount > 3 || spread > 700;
        if (!wasLiveStream && fullText) {
            await typewriterRender(botDiv, fullText, () => !isStreaming);
        }

        setStreamingState(false);
    } catch (error) {
        clearInterval(statusInterval);
        setStreamingState(false);
        currentReader = null;
        if (error.name === "AbortError" || error.name === "TypeError") {
            botDiv.innerHTML = renderMarkdown(botDiv.textContent || "") + " <em>[stopped]</em>";
            return;
        }
        botDiv.className = "bot-message error-message";
        botDiv.textContent = "Something went wrong while connecting to ENZO.";
    }
}

document.addEventListener("DOMContentLoaded", () => {
    byId("chatForm")?.addEventListener("submit", (event) => {
        event.preventDefault();
        if (!isActionFlow) sendMessage();
    });

    byId("messageInput")?.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey && !isActionFlow) {
            event.preventDefault();
            sendMessage();
        }
    });
}); 