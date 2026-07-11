# 无障碍服务触控方案（input 被拦截时的备选路径）

> 当系统/ROM 拒绝 `adb shell input` 注入（缺 `INJECT_EVENTS`，常见于未开「USB 调试(安全设置)」、或某些安全窗口）时，用 **Android AccessibilityService + `dispatchGesture`** 实现坐标点击/滑动。它**不依赖 INJECT_EVENTS 权限**，是可靠的兜底通道。

---

## 一、为什么能绕开拦截

| 方式 | 依赖权限 | 备注 |
|------|----------|------|
| `adb shell input tap x y` | `INJECT_EVENTS`（shell 默认有，但安全窗口/未开安全设置时被拒） | 当前主路径 |
| AccessibilityService.dispatchGesture | 仅需「已启用无障碍服务」 | 对**所有界面**（含锁屏外的安全窗口）都能注入，不挑权限 |

`dispatchGesture` 是 Android 官方给无障碍应用模拟手势的 API，系统信任无障碍服务，因此不受 INJECT_EVENTS 限制。

---

## 二、架构

```
phone-mcp (PC 侧)
   │  adb shell am broadcast -a com.phonemcp.a11y.TAP --ei x 381 --ei y 1366 --ei d 80
   ▼
手机侧 TapAccessibilityService（已启用）
   │  onReceive(action=TAP) → 用 (x,y) 构建 Path
   ▼
dispatchGesture(GestureDescription, callback, null)  →  系统注入一次点击
```

触发通道选 **广播（am broadcast）**：简单、无需建 socket、adb 即可发，延迟 ~几十 ms 可接受。

---

## 三、参考实现（Kotlin）

### 1) 服务本体 `TapAccessibilityService.kt`
```kotlin
package com.phonemcp.a11y

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.graphics.Path
import android.os.Build
import android.util.Log

class TapAccessibilityService : AccessibilityService() {

    companion object {
        const val ACTION_TAP = "com.phonemcp.a11y.TAP"
        const val ACTION_SWIPE = "com.phonemcp.a11y.SWIPE"
    }

    private val receiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            when (intent?.action) {
                ACTION_TAP -> {
                    val x = intent.getIntExtra("x", -1)
                    val y = intent.getIntExtra("y", -1)
                    val d = intent.getIntExtra("d", 80)
                    if (x >= 0 && y >= 0) tap(x, y, d)
                }
                ACTION_SWIPE -> {
                    val x1 = intent.getIntExtra("x1", -1)
                    val y1 = intent.getIntExtra("y1", -1)
                    val x2 = intent.getIntExtra("x2", -1)
                    val y2 = intent.getIntExtra("y2", -1)
                    val d = intent.getIntExtra("d", 300)
                    if (x1 >= 0 && y1 >= 0 && x2 >= 0 && y2 >= 0) swipe(x1, y1, x2, y2, d)
                }
            }
        }
    }

    override fun onServiceConnected() {
        super.onServiceConnected()
        val filter = IntentFilter().apply {
            addAction(ACTION_TAP)
            addAction(ACTION_SWIPE)
        }
        // 导出的receiver + 自定义权限，避免被任意App冒用
        registerReceiver(receiver, filter, "com.phonemcp.a11y.PERMISSION", null)
        Log.i("phone-mcp-a11y", "service connected")
    }

    override fun onDestroy() {
        super.onDestroy()
        try { unregisterReceiver(receiver) } catch (_: Exception) {}
    }

    private fun tap(x: Int, y: Int, durationMs: Int) {
        val path = Path().apply { moveTo(x.toFloat(), y.toFloat()) }
        val builder = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, durationMs.toLong()))
        dispatchGesture(builder.build(), null, null)
    }

    private fun swipe(x1: Int, y1: Int, x2: Int, y2: Int, durationMs: Int) {
        val path = Path().apply {
            moveTo(x1.toFloat(), y1.toFloat())
            lineTo(x2.toFloat(), y2.toFloat())
        }
        val builder = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, durationMs.toLong()))
        dispatchGesture(builder.build(), null, null)
    }

    override fun onAccessibilityEvent(event: android.view.accessibility.AccessibilityEvent?) {}
    override fun onInterrupt() {}
}
```

### 2) `AndroidManifest.xml`（关键片段）
```xml
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="com.phonemcp.a11y">
    <permission android:name="com.phonemcp.a11y.PERMISSION"
        android:protectionLevel="signature" />

    <application>
        <service
            android:name=".TapAccessibilityService"
            android:permission="android.permission.BIND_ACCESSIBILITY_SERVICE"
            android:exported="true">
            <intent-filter>
                <action android:name="android.accessibilityservice.AccessibilityService" />
            </intent-filter>
            <meta-data
                android:name="android.accessibilityservice"
                android:resource="@xml/a11y_service" />
        </service>
    </application>
</manifest>
```

### 3) `res/xml/a11y_service.xml`
```xml
<accessibility-service xmlns:android="http://schemas.android.com/apk/res/android"
    android:description="@string/a11y_desc"
    android:accessibilityFeedbackType="feedbackGeneric"
    android:accessibilityFlags="flagRequestTouchExplorationMode"
    android:canPerformGestures="true"
    android:notificationTimeout="0" />
```
> 关键点：`android:canPerformGestures="true"` 是 `dispatchGesture` 生效的硬前提。

---

## 四、phone-mcp 侧怎么调（已预埋）

`server.py` 已加 `phone_a11y_tap` 工具 + `a11y_tap()` helper，发送广播：
```python
run_adb(["shell","am","broadcast","-a","com.phonemcp.a11y.TAP",
         "--ei","x",str(x),"--ei","y",str(y),"--ei","d",str(duration_ms)], ...)
```
等价命令：
```bash
adb shell am broadcast -a com.phonemcp.a11y.TAP --ei x 381 --ei y 1366 --ei d 80
```

---

## 五、启用步骤（一次性）

1. 用 Android Studio / Gradle 构建上面的 APK，安装到手机（`adb install`）。
2. 设置 → 更多设置 → 无障碍 → 找到「phone-mcp a11y」→ 开启。
3. 之后对话里即可用 `phone_a11y_tap`（或 MCP 框架）点击，绕过 input 拦截。

---

## 六、局限与注意

- **手势有最短时长**：`dispatchGesture` 单次 stroke 建议 ≥ 50–80ms，否则可能丢；比 `input tap` 略慢但稳定。
- **一次一个手势**：连续多点需排队发送广播。
- **必须用户手动开启无障碍**：系统强制，无法 adb 静默开启（安全限制）。
- **坐标基于屏幕像素**：与 `input tap` 同一坐标系，复用现有 OCR/UI 定位结果即可。
- **权限保护**：receiver 用 `signature` 级自定义权限，避免被第三方 App 冒用触发点击。

---

## 七、何时切到这条路径

`phone_find_text` / `phone_tap_text` 定位到坐标后，若 `input tap` 返回非 0（被拦），自动改用 `phone_a11y_tap`（需服务已装）。当前本机 `input` 可用，故日常仍走主路径；本方案作为**兜底**随时待命。
