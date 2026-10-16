package local.videoqa.clipboard;

import android.app.Activity;
import android.content.ClipData;
import android.content.ClipDescription;
import android.content.ClipboardManager;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.view.Gravity;
import android.widget.TextView;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.json.JSONObject;

/** Reads only the fresh Shopee URL requested by the local scanner, while in the foreground. */
public final class ClipboardActivity extends Activity {
    private final Handler handler = new Handler(Looper.getMainLooper());
    private long deadline;
    private long since;
    private String request;
    private boolean done;
    private final Runnable read = this::readLink;

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        request = getIntent().getStringExtra("request");
        since = getIntent().getLongExtra("since", Long.MAX_VALUE);
        deadline = SystemClock.uptimeMillis() + 4500;
        TextView message = new TextView(this);
        message.setText("正在读取刚刚复制的视频链接…\n完成后自动返回 Shopee Seller");
        message.setTextSize(17);
        message.setGravity(Gravity.CENTER);
        setContentView(message);
        handler.postDelayed(() -> complete("error", "", 0, "剪贴板读取超时，请重试当前视频。"), 5000);
    }

    @Override public void onWindowFocusChanged(boolean focused) {
        super.onWindowFocusChanged(focused);
        if (focused && !done) {
            handler.removeCallbacks(read);
            handler.post(read);
        }
    }

    private void readLink() {
        if (done || !hasWindowFocus()) return;
        try {
            ClipboardManager manager = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
            ClipDescription description = manager.getPrimaryClipDescription();
            if (description != null && description.getTimestamp() >= since) {
                ClipData data = manager.getPrimaryClip();
                if (data != null) {
                    for (int i = 0; i < data.getItemCount(); i++) {
                        CharSequence text = data.getItemAt(i).getText();
                        if (text == null) continue;
                        Matcher urls = Pattern.compile("https?://[^\\s<>\\\"\\u3000]+").matcher(text);
                        while (urls.find()) {
                            String link = urls.group();
                            String host = Uri.parse(link).getHost();
                            if (host == null) continue;
                            host = host.toLowerCase(Locale.ROOT);
                            if (host.equals("shp.ee") || host.endsWith(".shp.ee")
                                    || host.matches("(?:[a-z0-9-]+\\.)*shopee\\.(?:com\\.(?:my|br|mx|co|ar)|co\\.(?:id|th)|sg|my|vn|ph|tw|cl|pl)")) {
                                complete("ok", link, description.getTimestamp(), "");
                                return;
                            }
                        }
                    }
                }
            }
        } catch (Exception error) {
            complete("error", "", 0, "无法读取本次复制的视频链接：" + error.getClass().getSimpleName());
            return;
        }
        if (SystemClock.uptimeMillis() >= deadline) {
            complete("error", "", 0, "没有读到本次 Copy Link 的新链接，请重试当前视频。");
        } else {
            handler.postDelayed(read, 150);
        }
    }

    private void complete(String status, String link, long timestamp, String error) {
        if (done) return;
        done = true;
        handler.removeCallbacksAndMessages(null);
        try {
            JSONObject result = new JSONObject().put("request", request).put("status", status)
                    .put("link", link).put("copied_at", timestamp).put("error", error);
            File temporary = new File(getFilesDir(), "clipboard.tmp");
            try (FileOutputStream output = new FileOutputStream(temporary)) {
                output.write(result.toString().getBytes(StandardCharsets.UTF_8));
            }
            temporary.renameTo(new File(getFilesDir(), "clipboard.json"));
        } catch (Exception ignored) { }
        finishAndRemoveTask();
    }

    @Override public void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        super.onDestroy();
    }
}
