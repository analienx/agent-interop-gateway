package io.github.analienx.aigwbridge;

import android.content.Context;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.List;

final class GatewayClient {
    private static final int MAX_RESPONSE_CHARS = 200_000;
    private static final long POLL_BUDGET_MS = 20_000;
    private static final long POLL_INTERVAL_MS = 750;

    private GatewayClient() {}

    static String submit(Context context, String delegationId, String task) throws Exception {
        String baseUrl = ConfigStore.validateGatewayUrl(
                ConfigStore.getGatewayUrl(context),
                ConfigStore.isInsecureLanAllowed(context)
        );
        List<String> capabilities = ConfigStore.getCapabilities(context);
        if (capabilities.isEmpty()) {
            throw new IllegalStateException("No delegation capabilities are configured.");
        }
        String risk = ConfigStore.getRisk(context);

        JSONObject routing = new JSONObject()
                .put("preference", "local_first")
                .put("allow_fallback", true)
                .put("fallback_on_failure", risk.equals("read"));
        JSONArray caps = new JSONArray();
        for (String capability : capabilities) {
            caps.put(capability);
        }
        JSONObject body = new JSONObject()
                .put("protocol", "aigw/1")
                .put("id", delegationId)
                .put("task", task)
                .put("origin", new JSONObject().put("surface", "android-accessibility-companion"))
                .put("capabilities", caps)
                .put("risk", risk)
                .put("routing", routing);

        String token = SecretStore.loadToken(context);
        JSONObject result;
        try {
            result = requestJson(
                    baseUrl + "/v1/delegations",
                    "POST",
                    body,
                    token,
                    true,
                    30_000
            );
        } catch (GatewayHttpException exc) {
            if (exc.status >= 400 && exc.status < 500) {
                return failure(delegationId, exc.getMessage()).toString();
            }
            throw exc;
        }

        long deadline = System.currentTimeMillis() + POLL_BUDGET_MS;
        while (isInProgress(result)) {
            if (System.currentTimeMillis() >= deadline) {
                throw new IOException(
                        "Delegation is still running; the durable id will be resumed later."
                );
            }
            Thread.sleep(POLL_INTERVAL_MS);
            try {
                result = requestJson(
                        baseUrl + "/v1/delegations/" + delegationId,
                        "GET",
                        null,
                        token,
                        false,
                        15_000
                );
            } catch (GatewayHttpException exc) {
                if (exc.status >= 400 && exc.status < 500 && exc.status != 404) {
                    return failure(delegationId, exc.getMessage()).toString();
                }
                throw exc;
            }
        }
        return result.toString();
    }

    static String diagnose(Context context) throws Exception {
        String baseUrl = ConfigStore.validateGatewayUrl(
                ConfigStore.getGatewayUrl(context),
                ConfigStore.isInsecureLanAllowed(context)
        );
        String token = SecretStore.loadToken(context);
        JSONObject health = requestJson(
                baseUrl + "/health", "GET", null, token, false, 10_000
        );
        JSONObject ready = requestJson(
                baseUrl + "/ready", "GET", null, token, false, 15_000
        );
        if (!"ok".equals(health.optString("status"))) {
            throw new IOException("Gateway health response is not OK.");
        }
        if (!ready.optBoolean("ready", false)) {
            throw new IOException("Gateway execution plane is not ready: " + ready);
        }
        int readyExecutors = ready.optInt("ready_executors", 0);
        String version = health.optString("version", "unknown");
        return "Gateway " + version + " ready; executors=" + readyExecutors;
    }

    private static boolean isInProgress(JSONObject result) {
        String state = result.optString("state", "");
        return state.equals("queued") || state.equals("running");
    }

    private static JSONObject requestJson(
            String url,
            String method,
            JSONObject body,
            String token,
            boolean preferAsync,
            int readTimeoutMs
    ) throws Exception {
        byte[] payload = body == null
                ? null
                : body.toString().getBytes(StandardCharsets.UTF_8);
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        try {
            connection.setConnectTimeout(10_000);
            connection.setReadTimeout(readTimeoutMs);
            connection.setRequestMethod(method);
            connection.setRequestProperty("Accept", "application/json");
            if (preferAsync) {
                connection.setRequestProperty("Prefer", "respond-async");
            }
            if (!token.isEmpty()) {
                connection.setRequestProperty("Authorization", "Bearer " + token);
            }
            if (payload != null) {
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                connection.setDoOutput(true);
                connection.setFixedLengthStreamingMode(payload.length);
                try (OutputStream output = connection.getOutputStream()) {
                    output.write(payload);
                }
            }

            int status = connection.getResponseCode();
            InputStream stream = status >= 400
                    ? connection.getErrorStream()
                    : connection.getInputStream();
            String response = readLimited(stream);
            if (status < 200 || status >= 300) {
                throw new GatewayHttpException(status, "Gateway HTTP " + status + ": " + response);
            }
            return new JSONObject(response);
        } finally {
            connection.disconnect();
        }
    }

    private static JSONObject failure(String delegationId, String error) throws Exception {
        return new JSONObject()
                .put("protocol", "aigw/1")
                .put("delegation_id", delegationId)
                .put("state", "failed")
                .put("error", error);
    }

    private static String readLimited(InputStream stream) throws IOException {
        if (stream == null) {
            return "";
        }
        StringBuilder builder = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(
                new InputStreamReader(stream, StandardCharsets.UTF_8)
        )) {
            char[] buffer = new char[4096];
            int read;
            while ((read = reader.read(buffer)) >= 0) {
                if (builder.length() + read > MAX_RESPONSE_CHARS) {
                    throw new IOException("Gateway response exceeded safety limit.");
                }
                builder.append(buffer, 0, read);
            }
        }
        return builder.toString();
    }

    static String formatResult(String resultJson, String delegationId) throws Exception {
        JSONObject result = new JSONObject(resultJson);
        String state = result.optString("state", "unknown");
        String rendered;
        if (state.equals("succeeded")) {
            String output = result.optString("stdout", "").trim();
            if (output.isEmpty()) {
                output = result.optJSONObject("payload") == null
                        ? "completed"
                        : result.getJSONObject("payload").toString();
            }
            rendered = marker(delegationId) + " " + output;
        } else {
            String error = result.optString("error", "");
            if (error.isEmpty()) {
                error = result.optString("stderr", state);
            }
            rendered = marker(delegationId) + " FAILED: " + error;
        }
        int limit = 6000;
        if (rendered.length() > limit) {
            rendered = rendered.substring(0, limit) + " ...[mobile relay truncated]";
        }
        return rendered;
    }

    static String marker(String delegationId) {
        String suffix = delegationId.length() <= 8
                ? delegationId
                : delegationId.substring(delegationId.length() - 8);
        return "[local delegation result " + suffix + "]";
    }

    private static final class GatewayHttpException extends IOException {
        final int status;

        GatewayHttpException(int status, String message) {
            super(message);
            this.status = status;
        }
    }
}
