ATMS CONFERENCE BOOKING - TEAM SETUP
====================================

FILES (keep all in the same folder)
  atms_conference_booking.html   the booking page
  atms_booking_server.py         small server that shares bookings with everyone

ONE-TIME SETUP (on one office PC that stays on during working hours)
  1. Install Python 3.8 or later from python.org (tick "Add Python to PATH").
  2. Open the folder and run:   python atms_booking_server.py
  3. The window prints an address such as  http://192.168.1.25:8000
  4. Share that address with the team. Everyone opens it in a browser
     (Chrome / Edge / Safari, PC or phone on the office Wi-Fi).
  5. If others cannot connect, allow Python through Windows Firewall
     (private network) when prompted.

DAILY USE
  - Each person enters their name once; it appears on their bookings.
  - Bookings are live for all users (the page refreshes every 5 seconds).
  - The server blocks double bookings even if two people click together.
  - Hours: 9:00 AM to 7:00 PM, Monday to Saturday, up to 30 days ahead.
  - Admin tab (room administrator): default PIN 1234. Change it on first use.
    Note: the PIN is a convenience gate, not strong security.

DATA
  Bookings are saved in bookings.json next to the server script. Back it up
  if you want a history. To start fresh, stop the server and delete the file.

NOTES
  - Opening the .html file directly (double-click) works only in offline mode:
    bookings stay in that one browser and are NOT shared.
  - Fonts load from Google Fonts; without internet the page uses a default font.
  - Stop the server with Ctrl+C. Use another port with:
    python atms_booking_server.py 8080
