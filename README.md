FFmpeg GUI Wrapper



A lightweight, portable, and beginner-friendly desktop application for simple video editing and conversion, built with Python and PyQt6. It acts as a wrapper around FFmpeg, allowing users to crop, trim, rotate, flip, and join videos directly into standard MP4 files.



Prerequisites



Python 3.8+ installed on your system.



FFmpeg and FFprobe must be installed and available in your system's PATH (or placed in the same directory as the executable).



Download FFmpeg from: https://ffmpeg.org/download.html



Project Structure



ffmpeg\_gui\_project/

│

├── ffmpeg\_gui.py       # Main application source code (UI, Player, and Logic)

├── requirements.txt    # Python dependencies

└── README.md           # Instructions and documentation





Setup \& Virtual Environment



To keep your system clean, it is highly recommended to use a Python virtual environment.



1\. Create a Virtual Environment:

Open your terminal/command prompt in the project folder and run:



python -m venv venv





2\. Activate the Virtual Environment:



Windows:



venv\\Scripts\\activate





macOS / Linux:



source venv/bin/activate





3\. Install Dependencies:



pip install -r requirements.txt





Running the Application



Ensure your virtual environment is active and run:



python ffmpeg\_gui.py





Creating a Portable Executable



To package this application into a portable, standalone executable that doesn't require users to install Python, you can use PyInstaller.



Install PyInstaller in your virtual environment:



pip install pyinstaller





Build the portable executable (this creates a single .exe file on Windows):



pyinstaller --noconsole --onefile ffmpeg\_gui.py





Your portable app will be located in the newly created dist/ folder.

(Note: Users will still need ffmpeg.exe and ffprobe.exe alongside the executable, or you can bundle them together using PyInstaller's --add-data flag).



Major Design Decisions



PyQt6 Framework: Chosen for its native QMediaPlayer which easily embeds video playback, handles millisecond-level seeking, and supports overlays for the visual crop tool.



Single File Structure for Code: The entire application logic (GUI, Video Player, FFmpeg subprocess wrapping) is encapsulated in ffmpeg\_gui.py using object-oriented principles. This ensures maximum portability and ease of compilation.



Strict MP4 Workflow: To prevent container conflicts, all operations trigger an MP4 H.264/AAC re-encode by default. This satisfies the "MP4 only" rule and ensures frame-accurate millisecond cuts, which stream-copying (-c copy) often fails to achieve.



Smart Joining: The join functionality automatically scales and pads videos to match the resolution of the first video in the list to prevent FFmpeg concat demuxer crashes when resolutions differ.

