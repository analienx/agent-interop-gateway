package io.github.analienx.aigwbridge;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import android.content.Context;

import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;

import org.junit.After;
import org.junit.Test;
import org.junit.runner.RunWith;

@RunWith(AndroidJUnit4.class)
public final class BridgeInstrumentedTest {
    private final Context context = ApplicationProvider.getApplicationContext();

    @After
    public void cleanUp() throws Exception {
        SecretStore.storeToken(context, "");
        new LedgerDb(context).clearAll();
    }

    @Test
    public void tokenRoundTripUsesAndroidKeystore() throws Exception {
        SecretStore.storeToken(context, "test-token-12345678901234567890");
        assertEquals("test-token-12345678901234567890", SecretStore.loadToken(context));
    }

    @Test
    public void relayLedgerPersistsState() {
        LedgerDb ledger = new LedgerDb(context);
        LedgerDb.Record claimed = ledger.claim("fingerprint", "delegation-id", "inspect repo");
        assertNotNull(claimed);
        assertEquals("pending", claimed.status);
        ledger.update("fingerprint", "result_ready", "{\"state\":\"succeeded\"}");
        LedgerDb.Record restored = ledger.get("fingerprint");
        assertNotNull(restored);
        assertEquals("result_ready", restored.status);
        assertTrue(restored.resultJson.contains("succeeded"));
        ledger.close();
    }

    @Test
    public void loopbackGatewayIsSafeDefault() {
        assertEquals("http://127.0.0.1:8765", ConfigStore.getGatewayUrl(context));
        assertEquals("read", ConfigStore.getRisk(context));
    }
}
