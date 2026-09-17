package io.github.analienx.aigwbridge;

import java.util.regex.Matcher;
import java.util.regex.Pattern;

final class TriggerParser {
    private static final Pattern TRIGGER = Pattern.compile(
            "^\\s*(?:delegate\\s+locally|local\\s+delegate|"
                    + "delegate\\s+(?:this\\s+)?to\\s+(?:the\\s+)?local\\s+machine)"
                    + "\\b[\\s:,-]*(.+)$",
            Pattern.CASE_INSENSITIVE | Pattern.DOTALL
    );

    private TriggerParser() {}

    static String extractTask(String text, int maxTaskChars) {
        String normalized = normalize(text);
        if (normalized.startsWith("[local delegation result")) {
            return null;
        }
        Matcher matcher = TRIGGER.matcher(normalized);
        if (!matcher.matches()) {
            return null;
        }
        String task = normalize(matcher.group(1));
        if (task.isEmpty() || task.length() > maxTaskChars) {
            return null;
        }
        return task;
    }

    static String normalize(String value) {
        return value == null ? "" : value.trim().replaceAll("\\s+", " ");
    }
}
