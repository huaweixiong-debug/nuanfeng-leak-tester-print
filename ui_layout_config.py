# -*- coding: utf-8 -*-
"""
极简UI布局配置文件
"""

# 默认只显示一个仪器控制卡片
DEFAULT_LAYOUT = {
    "card-instrument-control": {"width": 380, "height": 160, "left": 50, "top": 50},
}

# 定义唯一的卡片内容
CARD_CONTENTS = {
    'card-instrument-control': '''
        <div class="drag-handle cursor-move -mx-4 -mt-4 mb-3 p-3 rounded-t-2xl hover:bg-white/5 transition-colors relative" style="z-index: 20;">
            <h2 class="text-base font-semibold pointer-events-none flex items-center gap-2"><span class="w-6 h-6 rounded-md bg-indigo-500/20 flex items-center justify-center text-sm">🕹️</span>仪器控制</h2>
        </div>
        <div class="flex gap-4 h-full items-center justify-center pb-4">
            <button onclick="runInstrumentAction('start')" class="flex-1 px-4 py-3 rounded-xl btn-success text-lg font-medium transition-all flex items-center justify-center gap-2">
                启动
            </button>
            <button onclick="runInstrumentAction('stop')" class="flex-1 px-4 py-3 rounded-xl btn-danger text-lg font-medium transition-all flex items-center justify-center gap-2">
                停止
            </button>
        </div>
    '''
}

# 极简通用样式
COMMON_STYLES = '''<link href="https://unpkg.com/tailwindcss@^2/dist/tailwind.min.css" rel="stylesheet">
<style>
    body { background-color: #111827; /* 深灰色背景 */ }
    .glass { background: rgba(31, 41, 55, 0.7); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.1); }
    .btn-success { background: linear-gradient(to right, #10B981, #059669); color: white; }
    .btn-danger { background: linear-gradient(to right, #EF4444, #DC2626); color: white; }
    .draggable-card { box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -2px rgba(0, 0, 0, 0.05); }
</style>
'''

# 移除导航栏
NAV_BAR = ''''''

# 极简页面脚本
TEST_PAGE_SCRIPTS = '''
<script src="https://unpkg.com/interactjs/dist/interact.min.js"></script>
<script>
    // --- 提示信息 --- 
    function showToast(message, isSuccess) {
        var toast = document.createElement('div');
        toast.textContent = message;
        toast.className = 'fixed bottom-5 right-5 px-5 py-3 rounded-lg text-white text-sm font-semibold';
        toast.style.transition = 'opacity 0.3s ease';
        toast.style.background = isSuccess ? 'linear-gradient(to right, #10B981, #059669)' : 'linear-gradient(to right, #EF4444, #DC2626)';
        document.body.appendChild(toast);
        setTimeout(() => {
            toast.style.opacity = '0';
            setTimeout(() => document.body.removeChild(toast), 300);
        }, 3000);
    }

    // --- API 调用 --- 
    async function runInstrumentAction(action) {
        const endpoint = action === 'start' ? '/api/start_instrument' : '/api/stop_instrument';
        const actionText = action === 'start' ? '启动' : '停止';
        try {
            const response = await fetch(endpoint, { method: 'POST' });
            const result = await response.json();
            if (result.success) {
                showToast(`'${actionText}' 命令已成功发送。`, true);
            } else {
                showToast(`'${actionText}' 命令失败: ${result.message}`, false);
            }
        } catch (error) {
            showToast(`请求失败: ${error}`, false);
        }
    }

    // --- 卡片交互 --- 
    interact('.draggable-card').resizable({
        edges: { left: true, right: true, bottom: true, top: true },
        listeners: {
            move: function (event) {
                let target = event.target;
                let x = (parseFloat(target.getAttribute('data-x')) || 0);
                let y = (parseFloat(target.getAttribute('data-y')) || 0);
                target.style.width = event.rect.width + 'px';
                target.style.height = event.rect.height + 'px';
                x += event.deltaRect.left;
                y += event.deltaRect.top;
                target.style.transform = 'translate(' + x + 'px,' + y + 'px)';
                target.setAttribute('data-x', x);
                target.setAttribute('data-y', y);
            }
        }
    }).draggable({
        listeners: { move: window.dragMoveListener }
    });
</script>
'''
