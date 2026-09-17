package io.github.analienx.aigwbridge;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ComponentName;
import android.content.Intent;
import android.os.Bundle;
import android.provider.Settings;
import android.text.InputType;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

public final class MainActivity extends Activity {
    private EditText urlField;
    private EditText tokenField;
    private EditText capabilitiesField;
    private CheckBox insecureCheck;
    private CheckBox writeCheck;
    private TextView statusView;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        // The configuration screen can display the gateway bearer token.
        getWindow().setFlags(
                WindowManager.LayoutParams.FLAG_SECURE,
                WindowManager.LayoutParams.FLAG_SECURE
        );

        ScrollView scroll = new ScrollView(this);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(20);
        root.setPadding(pad, pad, pad, pad);
        scroll.addView(root);

        TextView title = new TextView(this);
        title.setText("Agent Interop Gateway — Android Bridge");
        title.setTextSize(22);
        root.addView(title);

        TextView summary = new TextView(this);
        summary.setText(
                "Event-driven bridge for ChatGPT. It reads the Android accessibility tree only; "
                        + "it does not record the screen. Normal execution requires an explicit phrase "
                        + "such as ‘delegate locally check the repo’."
        );
        summary.setPadding(0, dp(8), 0, dp(16));
        root.addView(summary);

        urlField = field("Gateway URL", ConfigStore.getGatewayUrl(this));
        root.addView(urlField);

        tokenField = field("Bearer token", SecretStore.loadToken(this));
        tokenField.setInputType(
                InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD
        );
        root.addView(tokenField);

        capabilitiesField = field(
                "Capabilities (comma separated)",
                ConfigStore.getCapabilitiesText(this)
        );
        root.addView(capabilitiesField);

        insecureCheck = new CheckBox(this);
        insecureCheck.setText("Allow plain HTTP to a non-loopback/LAN host (not recommended)");
        insecureCheck.setChecked(ConfigStore.isInsecureLanAllowed(this));
        root.addView(insecureCheck);

        writeCheck = new CheckBox(this);
        writeCheck.setText("Enable WRITE delegation mode (read-only is the safe default)");
        writeCheck.setChecked(ConfigStore.isWriteEnabled(this));
        root.addView(writeCheck);

        Button save = new Button(this);
        save.setText("Save configuration");
        save.setOnClickListener(view -> saveConfiguration());
        root.addView(save);

        Button testGateway = new Button(this);
        testGateway.setText("Test gateway + executor");
        testGateway.setOnClickListener(view -> testGateway(testGateway));
        root.addView(testGateway);

        Button accessibility = new Button(this);
        accessibility.setText("Open Accessibility settings");
        accessibility.setOnClickListener(
                view -> startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        );
        root.addView(accessibility);

        Button clearHistory = new Button(this);
        clearHistory.setText("Clear local relay history");
        clearHistory.setOnClickListener(view -> confirmClearHistory());
        root.addView(clearHistory);

        statusView = new TextView(this);
        statusView.setPadding(0, dp(16), 0, 0);
        root.addView(statusView);

        TextView networking = new TextView(this);
        networking.setText(
                "Recommended first test: keep Gateway URL at http://127.0.0.1:8765 and run "
                        + "‘adb reverse tcp:8765 tcp:8765’ on the gateway machine. This keeps the "
                        + "bearer token off the LAN. For untethered use, prefer HTTPS/VPN transport."
        );
        networking.setPadding(0, dp(20), 0, 0);
        root.addView(networking);

        setContentView(scroll);
    }

    @Override
    protected void onResume() {
        super.onResume();
        refreshStatus();
    }

    private EditText field(String hint, String value) {
        EditText field = new EditText(this);
        field.setHint(hint);
        field.setText(value);
        field.setSingleLine(true);
        return field;
    }

    private void saveConfiguration() {
        Runnable save = () -> {
            try {
                ConfigStore.save(
                        this,
                        urlField.getText().toString(),
                        tokenField.getText().toString(),
                        capabilitiesField.getText().toString(),
                        insecureCheck.isChecked(),
                        writeCheck.isChecked()
                );
                Toast.makeText(this, "Configuration saved", Toast.LENGTH_SHORT).show();
                refreshStatus();
            } catch (Exception exc) {
                new AlertDialog.Builder(this)
                        .setTitle("Configuration rejected")
                        .setMessage(exc.getMessage())
                        .setPositiveButton("OK", null)
                        .show();
            }
        };

        if (writeCheck.isChecked() && !ConfigStore.isWriteEnabled(this)) {
            new AlertDialog.Builder(this)
                    .setTitle("Enable write delegation?")
                    .setMessage(
                            "WRITE mode can change files and machine state when the gateway and "
                                    + "selected executor also permit writes. Keep this disabled for "
                                    + "initial interoperability testing."
                    )
                    .setNegativeButton("Cancel", null)
                    .setPositiveButton("Enable", (dialog, which) -> save.run())
                    .show();
        } else {
            save.run();
        }
    }

    private void testGateway(Button button) {
        button.setEnabled(false);
        statusView.setText("Gateway diagnostic: testing...");
        new Thread(() -> {
            try {
                String diagnosis = GatewayClient.diagnose(this);
                runOnUiThread(() -> {
                    statusView.setText("Gateway diagnostic: " + diagnosis);
                    button.setEnabled(true);
                });
            } catch (Exception exc) {
                String message = exc.getMessage() == null
                        ? exc.getClass().getSimpleName()
                        : exc.getMessage();
                runOnUiThread(() -> {
                    statusView.setText("Gateway diagnostic: FAILED\n" + message);
                    button.setEnabled(true);
                });
            }
        }, "aigw-gateway-diagnostic").start();
    }

    private void confirmClearHistory() {
        new AlertDialog.Builder(this)
                .setTitle("Clear relay history?")
                .setMessage(
                        "This removes local duplicate-execution protection for previously seen "
                                + "delegation phrases. Only clear it when you intentionally need to "
                                + "repeat an identical command."
                )
                .setNegativeButton("Cancel", null)
                .setPositiveButton("Clear", (dialog, which) -> {
                    try (LedgerDb history = new LedgerDb(this)) {
                        history.clearAll();
                    }
                    Toast.makeText(this, "Relay history cleared", Toast.LENGTH_SHORT).show();
                })
                .show();
    }

    private void refreshStatus() {
        boolean enabled = isAccessibilityServiceEnabled();
        String mode = ConfigStore.isWriteEnabled(this) ? "WRITE" : "READ-ONLY";
        statusView.setText(
                "Accessibility relay: " + (enabled ? "ENABLED" : "DISABLED")
                        + "\nDelegation mode: " + mode
                        + "\nTrigger: delegate locally … / local delegate …"
        );
    }

    private boolean isAccessibilityServiceEnabled() {
        String expected = new ComponentName(this, RelayAccessibilityService.class).flattenToString();
        String enabled = Settings.Secure.getString(
                getContentResolver(),
                Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES
        );
        if (enabled == null) {
            return false;
        }
        for (String item : enabled.split(":")) {
            if (expected.equalsIgnoreCase(item)) {
                return true;
            }
        }
        return false;
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
