# Django PDF API – Setup & Run Guide

This document provides **step-by-step instructions** to set up and run this Django project on **Windows** and **Ubuntu (Linux)** using a **Python virtual environment**, including **GTK3 installation** required for **WeasyPrint**.

---

## 📋 Prerequisites (Windows & Ubuntu)

* **Python 3.10 – 3.12** (recommended)
* **pip** (comes with Python)
* **Git**

Check versions:

```bash
python --version
pip --version
```

---

## 📁 Project Structure (Simplified)

```text
pdf_api/
│── api/
│── blog/
│── venv/            # Virtual environment (created locally)
│── manage.py
│── requirements.txt
│── README.md
```

---

# 🪟 Windows Setup Guide

## 1️⃣ Install Python

1. Download Python from:
   👉 [https://www.python.org/downloads/](https://www.python.org/downloads/)
2. During installation:

   * ✅ Check **Add Python to PATH**
   * ✅ Install for current user

Verify installation:

```bat
python --version
```

---

## 2️⃣ Create Virtual Environment

From the project root directory:

```bat
py -m venv venv
```

---

## 3️⃣ Activate Virtual Environment

### Command Prompt (cmd)

```bat
venv\Scripts\activate
```

### PowerShell

```powershell
venv\Scripts\Activate.ps1
```

If PowerShell blocks execution (run once):

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

Verify virtual environment:

```bat
where python
```

Expected output:

```text
...\venv\Scripts\python.exe
```

---

## 4️⃣ Install Python Dependencies

```bat
pip install --upgrade pip
pip install -r requirements.txt
```

---

## 5️⃣ Install GTK3 (Required for WeasyPrint – Windows)

WeasyPrint depends on native GTK libraries on Windows.

### Recommended Method

1. Download GTK3 Runtime Installer:
   👉 [https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer](https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer)

2. Download the latest:

```text
gtk3-runtime-3.x.x-x64.exe
```

3. Install and:

   * ✅ Allow **Add GTK to PATH** when prompted

4. **Restart your terminal** after installation

---

## 6️⃣ Run Django Server (Windows)

```bat
python manage.py migrate
python manage.py runserver
```

Access the application:

```text
http://127.0.0.1:8000/
```

---

# 🐧 Ubuntu (Linux) Setup Guide

## 1️⃣ Install System Dependencies

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip \
  libcairo2 libpango-1.0-0 libpangocairo-1.0-0 \
  libgdk-pixbuf2.0-0 libffi-dev shared-mime-info
```

Verify Python installation:

```bash
python3 --version
```

---

## 2️⃣ Create Virtual Environment

```bash
python3 -m venv venv
```

---

## 3️⃣ Activate Virtual Environment

```bash
source venv/bin/activate
```

Verify virtual environment:

```bash
which python
```

Expected output:

```text
.../venv/bin/python
```

---

## 4️⃣ Install Python Dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

---

## 5️⃣ Run Django Server (Ubuntu)

```bash
python manage.py migrate
python manage.py runserver
```

Access the application:

```text
http://127.0.0.1:8000/
```

---

# 🔄 Common Virtual Environment Commands

Deactivate virtual environment:

```bash
deactivate
```

Remove virtual environment (recreate if broken):

### Windows

```bat
rmdir /s /q venv
```

### Ubuntu

```bash
rm -rf venv
```

---

# ⚠️ Important Notes

* ✅ Always activate the virtual environment before running the project
* ✅ GTK is mandatory for PDF rendering with WeasyPrint
* ❌ Avoid using unsupported Python versions on Windows

---

# 📦 requirements.txt (Example)

```text
Django>=4.2
WeasyPrint>=61.0
```

---

## ✅ You are ready to run the project!

If you encounter issues:

* Check Python version
* Verify GTK installation
* Recreate the virtual environment

---

Happy coding 🚀
