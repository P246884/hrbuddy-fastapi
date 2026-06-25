console.log("ENZO chat loaded");

let hrToken = "";
let pendingActionContext = null;
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
        renderGreeting(name ? name.split(/\s+/)[0] : "");
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

    const recognition = new SpeechRecognition();
    recognition.lang = "en-IN";
    recognition.interimResults = false;
    recognition.maxAlternatives = 1;
    recognition.start();
    recognition.onresult = (event) => {
        byId("messageInput").value = event.results[0][0].transcript;
        sendMessage();
    };
    recognition.onerror = (event) => console.log(event.error);
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
                <label for="halfDay_${pickerId}">Include half day</label>
            </div>
            <div id="halfDayOptions_${pickerId}" class="halfday-options">
                <div class="halfday-group">
                    <div class="halfday-label">Start date</div>
                    <div class="halfday-buttons">
                        <button type="button" class="half-option-btn selected" id="startFull_${pickerId}" onclick="selectStartHalf('${pickerId}', this)">Full Day</button>
                        <button type="button" class="half-option-btn" id="startSecond_${pickerId}" onclick="selectStartHalf('${pickerId}', this)">Second Half</button>
                    </div>
                </div>
                <div class="halfday-group">
                    <div class="halfday-label">End date</div>
                    <div class="halfday-buttons">
                        <button type="button" class="half-option-btn selected" id="endFull_${pickerId}" onclick="selectEndHalf('${pickerId}', this)">Full Day</button>
                        <button type="button" class="half-option-btn" id="endFirst_${pickerId}" onclick="selectEndHalf('${pickerId}', this)">First Half</button>
                    </div>
                </div>
            </div>
        </div>
        <div id="dateErr_${pickerId}" class="date-error">End date cannot be before start date.</div>
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
    wrapper.innerHTML = `
        <div class="action-title">${escapeHtml(data.message)}</div>
        <div class="reason-row">
            <textarea id="${rid}" placeholder="Enter reason (optional)..." rows="3"></textarea>
        </div>
        <button type="button" class="confirm-btn" onclick="confirmReason('${rid}')">Submit</button>
    `;
    appendMessage(wrapper);
    pendingActionContext = data.context;
    blockInput();
}

function renderLeavePicker(data) {
    const wrapper = makeMessage("bot-message action-card");
    const title = document.createElement("div");
    title.className = "action-title";
    title.textContent = data.message;
    wrapper.appendChild(title);

    const list = document.createElement("div");
    list.className = "leave-options";
    (data.leaves || []).forEach((leave) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "leave-option-btn";
        button.textContent = leave.label;
        button.addEventListener("click", () => selectLeaveForAction(leave.leave_guid, data.action, button));
        list.appendChild(button);
    });
    wrapper.appendChild(list);
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

        if (from === to && startSecond && endFirst) {
            return showError("Cannot select Second Half start and First Half end on the same date.");
        }

        if (startSecond) totalDays -= 0.5;
        if (endFirst) totalDays -= 0.5;

        const halfInfo = [];
        if (startSecond) halfInfo.push("start:second");
        if (endFirst) halfInfo.push("end:first");
        pendingActionContext.half_day_info = halfInfo.join(",");
    } else {
        pendingActionContext.half_day_info = null;
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
    appendMessage(wrapper);
    renderMore(); // first page
    unblockInput();
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

    try {
        const response = await fetch(CHAT_ENDPOINT, {
            method: "POST",
            headers: { "Content-Type": "application/json", "Authorization": "Bearer " + hrToken },
            body: JSON.stringify({ message })
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