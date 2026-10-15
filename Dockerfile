#整体流程可以理解为：
 #准备 Python 环境
 #    ↓
 #进入 /app 目录
 #    ↓
 #复制依赖清单
 #    ↓
 #安装依赖
 #    ↓
 #复制项目代码
 #    ↓
 #启动 python main.py

# 1. 底座：沿用刚才那个 python 精简版
FROM python:3.14-slim

# 2. 镜像里的工作目录
WORKDIR /app

# 3. 先只复制"依赖清单"这一个文件
COPY requirements.txt .

# 4. 在镜像里装依赖（装进镜像的系统 Python，不是 .venv）
RUN pip install --no-cache-dir -r requirements.txt

# 5. 再把其余代码复制进去
COPY . .

# 6. 容器启动时执行：先建库建表（init_db 幂等），再起服务
CMD ["sh", "-c", "python init_db.py && python main.py"]