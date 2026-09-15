package io.github.analienx.aigwbridge;

import android.content.Context;
import android.content.SharedPreferences;

import java.net.URI;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

final class ConfigStore {
    private static final String PREFS = "aigw_config";
    private static final String KEY_URL = "gateway_url";
    private static final String KEY_CAPS = "capabilities";
    private static final String KEY_INSECURE = "allow_insecure_lan";
    private static final String KEY_WRITE = "write_enabled";

    private ConfigStore() {}

    static SharedPreferences prefs(Context context) {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    static String getGatewayUrl(Context context) {
        return prefs(context).getString(KEY_URL, "http://127.0.0.1:8765");
    }

    static String getCapabilitiesText(Context context) {
        return prefs(context).getString(KEY_CAPS, "fs.read");
    }

    static List<String> getCapabilities(Context context) {
        return parseCapabilities(getCapabilitiesText(context));
    }

    static boolean isInsecureLanAllowed(Context context) {
        return prefs(context).getBoolean(KEY_INSECURE, false);
    }

    static boolean isWriteEnabled(Context context) {
        return prefs(context).getBoolean(KEY_WRITE, false);
    }

    static String getRisk(Context context) {
        return isWriteEnabled(context) ? "write" : "read";
    }

    static void save(
            Context context,
            String url,
            String token,
            String capabilities,
            boolean allowInsecureLan,
            boolean writeEnabled
    ) throws Exception {
        String normalizedUrl = validateGatewayUrl(url, allowInsecureLan);
        List<String> parsedCapabilities = parseCapabilities(capabilities);
        if (parsedCapabilities.isEmpty()) {
            throw new IllegalArgumentException("At least one capability is required.");
        }
        String normalizedToken = token.trim();
        URI gateway = URI.create(normalizedUrl);
        if ((!isLoopback(gateway.getHost()) || writeEnabled) && normalizedToken.length() < 24) {
            throw new IllegalArgumentException(
                    "A bearer token of at least 24 characters is required for LAN or WRITE mode."
            );
        }
        prefs(context).edit()
                .putString(KEY_URL, normalizedUrl)
                .putString(KEY_CAPS, capabilities.trim())
                .putBoolean(KEY_INSECURE, allowInsecureLan)
                .putBoolean(KEY_WRITE, writeEnabled)
                .apply();
        SecretStore.storeToken(context, normalizedToken);
    }

    static String validateGatewayUrl(String raw, boolean allowInsecureLan) {
        URI uri = URI.create(raw.trim());
        String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase(Locale.ROOT);
        if (!scheme.equals("http") && !scheme.equals("https")) {
            throw new IllegalArgumentException("Gateway URL must use http or https.");
        }
        if (uri.getHost() == null || uri.getHost().isEmpty()) {
            throw new IllegalArgumentException("Gateway URL must include a host.");
        }
        if (uri.getUserInfo() != null || uri.getQuery() != null || uri.getFragment() != null) {
            throw new IllegalArgumentException(
                    "Gateway URL must not contain credentials, a query, or a fragment."
            );
        }
        String path = uri.getPath();
        if (path != null && !path.isEmpty() && !path.equals("/")) {
            throw new IllegalArgumentException("Gateway URL must not contain a path.");
        }
        if (scheme.equals("http") && !isLoopback(uri.getHost()) && !allowInsecureLan) {
            throw new IllegalArgumentException(
                    "Plain HTTP is allowed only over loopback by default. "
                            + "Use HTTPS or explicitly allow insecure LAN transport."
            );
        }
        String value = raw.trim();
        while (value.endsWith("/")) {
            value = value.substring(0, value.length() - 1);
        }
        return value;
    }

    private static List<String> parseCapabilities(String raw) {
        List<String> values = new ArrayList<>();
        for (String item : raw.split(",")) {
            String trimmed = item.trim();
            if (trimmed.isEmpty()) {
                continue;
            }
            if (trimmed.length() > 128 || values.size() >= 64) {
                throw new IllegalArgumentException("Capabilities are too long or too numerous.");
            }
            if (!values.contains(trimmed)) {
                values.add(trimmed);
            }
        }
        return values;
    }

    static boolean isLoopback(String host) {
        String normalized = host.toLowerCase(Locale.ROOT);
        return normalized.equals("127.0.0.1")
                || normalized.equals("localhost")
                || normalized.equals("::1")
                || normalized.equals("[::1]");
    }
}
