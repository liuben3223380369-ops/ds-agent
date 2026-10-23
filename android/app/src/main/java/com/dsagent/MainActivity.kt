package com.dsagent

import android.Manifest
import android.app.ActivityManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.text.method.ScrollingMovementMethod
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.chaquo.python.Python
import com.google.android.material.progressindicator.LinearProgressIndicator

class MainActivity : AppCompatActivity() {

    private lateinit var input: EditText
    private lateinit var apiKey: EditText
    private lateinit var btnRun: Button
    private lateinit var btnStop: Button
    private lateinit var out: TextView
    private lateinit var scroll: ScrollView
    private lateinit var progress: LinearProgressIndicator

    private val events = StringBuilder()

    /** 接收 Service 推来的输出行。用显式广播，避免跨应用泄露。 */
    private val receiver = object : BroadcastReceiver() {
        override fun onReceive(c: Context?, intent: Intent?) {
            val text = intent?.getStringExtra(AgentService.EXTRA_TEXT) ?: return
            val kind = intent.getStringExtra(AgentService.EXTRA_KIND) ?: "log"
            append(text, kind)
            if (kind == "end") setRunning(false)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        input = findViewById(R.id.input)
        apiKey = findViewById(R.id.apiKey)
        apiKey.setText(getApiKeyFromPrefs())
        btnRun = findViewById(R.id.btnRun)
        btnStop = findViewById(R.id.btnStop)
        out = findViewById(R.id.out)
        scroll = findViewById(R.id.scroll)
        progress = findViewById(R.id.progress)
        out.movementMethod = ScrollingMovementMethod()

        btnRun.setOnClickListener {
            saveApiKey(apiKey.text.toString().trim())
            startTask()
        }
        btnStop.setOnClickListener {
            stopService(Intent(this, AgentService::class.java))
            append("（已请求停止）", "warn")
            setRunning(false)
        }

        requestPermissionsIfNeeded()
        showEnv()
    }

    override fun onResume() {
        super.onResume()
        val f = IntentFilter(AgentService.ACTION_EVENT)
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(receiver, f, Context.RECEIVER_NOT_EXPORTED)
        } else {
            registerReceiver(receiver, f)
        }
        setRunning(isServiceRunning())
    }

    override fun onPause() {
        try { unregisterReceiver(receiver) } catch (_: Exception) {}
        super.onPause()
    }

    private fun startTask() {
        val task = input.text.toString().trim()
        if (task.isEmpty()) {
            Toast.makeText(this, "先写点什么", Toast.LENGTH_SHORT).show()
            return
        }
        if (getApiKeyFromPrefs().isBlank()) {
            Toast.makeText(this, "还没填 DeepSeek API Key", Toast.LENGTH_LONG).show()
            return
        }
        events.clear()
        out.text = ""
        setRunning(true)
        val i = Intent(this, AgentService::class.java).apply {
            action = AgentService.ACTION_START
            putExtra(AgentService.EXTRA_TASK, task)
            putExtra(AgentService.EXTRA_KEY, getApiKeyFromPrefs())
        }
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(i) else startService(i)
    }

    private fun getApiKeyFromPrefs(): String =
        getSharedPreferences("dsagent", MODE_PRIVATE).getString("api_key", "") ?: ""

    private fun saveApiKey(key: String) {
        getSharedPreferences("dsagent", MODE_PRIVATE).edit()
            .putString("api_key", key).apply()
    }

    private fun append(text: String, kind: String) {
        runOnUiThread {
            events.append(text).append('\n')
            // 只保留最近 2000 行，避免长任务把内存吃满
            val lines = events.toString().split('\n')
            val shown = if (lines.size > 2000) lines.takeLast(2000).joinToString("\n")
                        else events.toString()
            out.text = shown
            scroll.post { scroll.fullScroll(View.FOCUS_DOWN) }
        }
    }

    private fun setRunning(running: Boolean) {
        runOnUiThread {
            btnRun.isEnabled = !running
            btnStop.visibility = if (running) View.VISIBLE else View.GONE
            progress.visibility = if (running) View.VISIBLE else View.GONE
        }
    }

    private fun isServiceRunning(): Boolean {
        val am = getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
        @Suppress("DEPRECATION")
        for (s in am.getRunningServices(Int.MAX_VALUE)) {
            if (AgentService::class.java.name == s.service.className) return true
        }
        return false
    }

    /** 首次启动时把 Python 环境报告打出来，方便确认降级状态 */
    private fun showEnv() {
        Thread {
            try {
                val py = Python.getInstance()
                val bridge = py.getModule("agent_bridge")
                val info = bridge.callAttr("env_report", filesDir.absolutePath).toString()
                runOnUiThread { append(info, "sys") }
            } catch (e: Exception) {
                runOnUiThread { append("环境探测失败：${e.message}", "err") }
            }
        }.start()
    }

    private fun requestPermissionsIfNeeded() {
        val needed = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= 33) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
                needed.add(Manifest.permission.POST_NOTIFICATIONS)
            }
        }
        if (needed.isNotEmpty()) {
            ActivityCompat.requestPermissions(this, needed.toTypedArray(), 1001)
        }
    }
}
