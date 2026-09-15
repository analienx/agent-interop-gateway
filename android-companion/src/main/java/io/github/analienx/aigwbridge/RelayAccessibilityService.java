package io.github.analienx.aigwbridge;

import android.accessibilityservice.AccessibilityService;
import android.graphics.Rect;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

public final class RelayAccessibilityService extends AccessibilityService {
    private static final String TAG = "AIGWBridge";
    private static final String CHATGPT_PACKAGE = "com.openai.chatgpt";
    private static final long SCAN_THROTTLE_MS = 400;
    private static final long RETRY_BACKOFF_MS = 5_000;
    private static final int MAX_TREE_NODES = 5_000;
    private static final int MAX_TASK_CHARS = 20_000;

    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final AtomicBoolean scanScheduled = new AtomicBoolean(false);
    private final Set<String> processing = ConcurrentHashMap.newKeySet();
    private ExecutorService worker;
    private LedgerDb ledger;
    private boolean baselineEstablished = false;

    @Override
    protected void onServiceConnected() {
        super.onServiceConnected();
        worker = Executors.newSingleThreadExecutor(runnable -> {
            Thread thread = new Thread(runnable, "aigw-relay-worker");
            thread.setDaemon(true);
            return thread;
        });
        ledger = new LedgerDb(this);
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        if (event == null || event.getPackageName() == null) {
            return;
        }
        if (!CHATGPT_PACKAGE.contentEquals(event.getPackageName())) {
            return;
        }
        if (event.getEventType() == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) {
            // Entering a different ChatGPT surface must not execute old visible transcript text.
            baselineEstablished = false;
        }
        if (scanScheduled.compareAndSet(false, true)) {
            mainHandler.postDelayed(() -> {
                scanScheduled.set(false);
                scanVisibleTree();
            }, SCAN_THROTTLE_MS);
        }
    }

    @Override
    public void onInterrupt() {
        // Android calls this when accessibility feedback is interrupted. No action is required.
    }

    @Override
    public void onDestroy() {
        mainHandler.removeCallbacksAndMessages(null);
        if (worker != null) {
            worker.shutdownNow();
        }
        if (ledger != null) {
            ledger.close();
        }
        super.onDestroy();
    }

    private void scanVisibleTree() {
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (!isChatGptRoot(root)) {
            baselineEstablished = false;
            return;
        }
        resumeOutstanding();
        List<String> texts = visibleTexts(root);
        if (!baselineEstablished) {
            // Fail closed: content that was already on screen when the bridge starts or
            // switches ChatGPT surfaces is observation baseline, not a new command.
            baselineEstablished = true;
            return;
        }
        Set<String> seenTasks = new HashSet<>();
        for (String text : texts) {
            String task = extractTask(text);
            if (task == null || !seenTasks.add(task)) {
                continue;
            }
            dispatch(task);
        }
    }

    private void resumeOutstanding() {
        for (LedgerDb.Record record : ledger.listOutstanding(4)) {
            if (!processing.add(record.fingerprint)) {
                continue;
            }
            if (record.status.equals("pending")) {
                if (System.currentTimeMillis() - record.updatedAt < RETRY_BACKOFF_MS) {
                    processing.remove(record.fingerprint);
                    continue;
                }
                submitOrResume(record);
            } else if (record.status.equals("result_ready") && record.resultJson != null) {
                scheduleInjection(record, record.resultJson);
            } else {
                processing.remove(record.fingerprint);
            }
        }
    }

    private void dispatch(String task) {
        String fingerprint = sha256(normalize(task).toLowerCase(Locale.ROOT));
        if (!processing.add(fingerprint)) {
            return;
        }
        LedgerDb.Record existing = ledger.get(fingerprint);
        LedgerDb.Record record = existing;
        if (record == null) {
            String delegationId = "android-" + fingerprint.substring(0, 32);
            record = ledger.claim(fingerprint, delegationId, task);
        }
        if (record == null) {
            processing.remove(fingerprint);
            return;
        }

        switch (record.status) {
            case "injected":
            case "injecting":
            case "injection_uncertain":
                processing.remove(fingerprint);
                return;
            case "pending":
                if (existing != null
                        && System.currentTimeMillis() - record.updatedAt < RETRY_BACKOFF_MS) {
                    processing.remove(fingerprint);
                    return;
                }
                submitOrResume(record);
                return;
            case "result_ready":
                if (record.resultJson != null) {
                    scheduleInjection(record, record.resultJson);
                } else {
                    ledger.update(fingerprint, "injection_uncertain", null);
                    processing.remove(fingerprint);
                }
                return;
            default:
                processing.remove(fingerprint);
        }
    }

    private void submitOrResume(LedgerDb.Record record) {
        worker.execute(() -> {
            try {
                String resultJson = GatewayClient.submit(
                        this,
                        record.delegationId,
                        record.task
                );
                ledger.update(record.fingerprint, "result_ready", resultJson);
                scheduleInjection(record, resultJson);
            } catch (Exception exc) {
                ledger.touchPending(record.fingerprint);
                processing.remove(record.fingerprint);
                Log.w(TAG, "Gateway submission failed: " + exc.getClass().getSimpleName());
            }
        });
    }

    private void scheduleInjection(LedgerDb.Record record, String resultJson) {
        final String rendered;
        try {
            rendered = GatewayClient.formatResult(resultJson, record.delegationId);
        } catch (Exception exc) {
            ledger.update(record.fingerprint, "injection_uncertain", null);
            processing.remove(record.fingerprint);
            return;
        }
        mainHandler.post(() -> injectResult(record, rendered));
    }

    private void injectResult(LedgerDb.Record record, String rendered) {
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (!isChatGptRoot(root)) {
            processing.remove(record.fingerprint);
            return; // Leave result_ready so a later ChatGPT event can retry safely.
        }
        AccessibilityNodeInfo editor = findEditor(root);
        if (editor == null) {
            processing.remove(record.fingerprint);
            return; // No UI action was attempted; result_ready remains retryable.
        }
        CharSequence existingText = editor.getText();
        if (existingText != null && !normalize(existingText.toString()).isEmpty()) {
            // Never overwrite a draft the user is typing.
            processing.remove(record.fingerprint);
            return;
        }

        ledger.update(record.fingerprint, "injecting", null);
        Bundle arguments = new Bundle();
        arguments.putCharSequence(
                AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                rendered
        );
        boolean set = editor.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments);
        if (!set) {
            markInjectionUncertain(record.fingerprint);
            return;
        }
        mainHandler.postDelayed(() -> sendAndVerify(record, editor), 250);
    }

    private void sendAndVerify(LedgerDb.Record record, AccessibilityNodeInfo originalEditor) {
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (!isChatGptRoot(root)) {
            markInjectionUncertain(record.fingerprint);
            return;
        }
        AccessibilityNodeInfo send = findSend(root);
        boolean sent = send != null && send.performAction(AccessibilityNodeInfo.ACTION_CLICK);
        if (!sent && Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            sent = originalEditor.performAction(
                    AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.getId()
            );
        }
        if (!sent) {
            markInjectionUncertain(record.fingerprint);
            return;
        }

        String marker = GatewayClient.marker(record.delegationId);
        mainHandler.postDelayed(() -> {
            AccessibilityNodeInfo verifyRoot = getRootInActiveWindow();
            boolean confirmed = isChatGptRoot(verifyRoot)
                    && containsMarkerOutsideEditor(verifyRoot, marker);
            if (confirmed) {
                ledger.update(record.fingerprint, "injected", null);
            } else {
                ledger.update(record.fingerprint, "injection_uncertain", null);
            }
            processing.remove(record.fingerprint);
        }, 1_200);
    }

    private void markInjectionUncertain(String fingerprint) {
        ledger.update(fingerprint, "injection_uncertain", null);
        processing.remove(fingerprint);
    }

    private static String extractTask(String text) {
        return TriggerParser.extractTask(text, MAX_TASK_CHARS);
    }

    private static String normalize(String value) {
        return TriggerParser.normalize(value);
    }

    private static String sha256(String value) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256")
                    .digest(value.getBytes(StandardCharsets.UTF_8));
            StringBuilder builder = new StringBuilder(digest.length * 2);
            for (byte item : digest) {
                builder.append(String.format(Locale.ROOT, "%02x", item & 0xff));
            }
            return builder.toString();
        } catch (Exception exc) {
            throw new IllegalStateException(exc);
        }
    }

    private static boolean isChatGptRoot(AccessibilityNodeInfo root) {
        return root != null
                && root.getPackageName() != null
                && CHATGPT_PACKAGE.contentEquals(root.getPackageName());
    }

    private static List<String> visibleTexts(AccessibilityNodeInfo root) {
        List<String> texts = new ArrayList<>();
        ArrayDeque<AccessibilityNodeInfo> queue = new ArrayDeque<>();
        queue.add(root);
        int visited = 0;
        while (!queue.isEmpty() && visited++ < MAX_TREE_NODES) {
            AccessibilityNodeInfo node = queue.removeFirst();
            if (node.isVisibleToUser() && node.getText() != null) {
                String text = normalize(node.getText().toString());
                if (!text.isEmpty()) {
                    texts.add(text);
                }
            }
            for (int index = 0; index < node.getChildCount(); index++) {
                AccessibilityNodeInfo child = node.getChild(index);
                if (child != null) {
                    queue.addLast(child);
                }
            }
        }
        return texts;
    }

    private static boolean containsMarkerOutsideEditor(
            AccessibilityNodeInfo root,
            String marker
    ) {
        ArrayDeque<AccessibilityNodeInfo> queue = new ArrayDeque<>();
        queue.add(root);
        int visited = 0;
        while (!queue.isEmpty() && visited++ < MAX_TREE_NODES) {
            AccessibilityNodeInfo node = queue.removeFirst();
            if (node.isVisibleToUser() && !node.isEditable() && node.getText() != null
                    && node.getText().toString().contains(marker)) {
                return true;
            }
            for (int index = 0; index < node.getChildCount(); index++) {
                AccessibilityNodeInfo child = node.getChild(index);
                if (child != null) {
                    queue.addLast(child);
                }
            }
        }
        return false;
    }

    private static AccessibilityNodeInfo findEditor(AccessibilityNodeInfo root) {
        ArrayDeque<AccessibilityNodeInfo> queue = new ArrayDeque<>();
        queue.add(root);
        AccessibilityNodeInfo best = null;
        int bestScore = Integer.MIN_VALUE;
        int visited = 0;
        Rect bounds = new Rect();
        while (!queue.isEmpty() && visited++ < MAX_TREE_NODES) {
            AccessibilityNodeInfo node = queue.removeFirst();
            if (node.isVisibleToUser() && node.isEnabled() && node.isEditable()) {
                node.getBoundsInScreen(bounds);
                int score = bounds.bottom + (node.isFocused() ? 100_000 : 0);
                if (score > bestScore) {
                    best = node;
                    bestScore = score;
                }
            }
            for (int index = 0; index < node.getChildCount(); index++) {
                AccessibilityNodeInfo child = node.getChild(index);
                if (child != null) {
                    queue.addLast(child);
                }
            }
        }
        return best;
    }

    private static AccessibilityNodeInfo findSend(AccessibilityNodeInfo root) {
        ArrayDeque<AccessibilityNodeInfo> queue = new ArrayDeque<>();
        queue.add(root);
        AccessibilityNodeInfo best = null;
        int bestScore = 0;
        int visited = 0;
        Rect bounds = new Rect();
        while (!queue.isEmpty() && visited++ < MAX_TREE_NODES) {
            AccessibilityNodeInfo node = queue.removeFirst();
            if (node.isVisibleToUser() && node.isEnabled() && node.isClickable()) {
                String text = node.getText() == null ? "" : node.getText().toString();
                String description = node.getContentDescription() == null
                        ? "" : node.getContentDescription().toString();
                String viewId = node.getViewIdResourceName() == null
                        ? "" : node.getViewIdResourceName();
                String label = (text + " " + description).trim().toLowerCase(Locale.ROOT);
                int score = 0;
                if (label.equals("send") || label.equals("send message")
                        || label.equals("odeslat") || label.equals("odeslat zprávu")) {
                    score += 8;
                } else if (label.contains("send") || label.contains("odeslat")) {
                    score += 4;
                }
                if (viewId.toLowerCase(Locale.ROOT).contains("send")) {
                    score += 6;
                }
                node.getBoundsInScreen(bounds);
                score += Math.max(0, bounds.bottom / 10_000);
                if (score > bestScore) {
                    best = node;
                    bestScore = score;
                }
            }
            for (int index = 0; index < node.getChildCount(); index++) {
                AccessibilityNodeInfo child = node.getChild(index);
                if (child != null) {
                    queue.addLast(child);
                }
            }
        }
        return best;
    }
}
