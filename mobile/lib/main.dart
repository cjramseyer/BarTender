import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'screens/home_screen.dart';
import 'screens/setup_screen.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final prefs = await SharedPreferences.getInstance();
  final serverUrl = prefs.getString('server_url') ?? '';
  runApp(BarTenderApp(initialUrl: serverUrl));
}

class BarTenderApp extends StatelessWidget {
  final String initialUrl;

  const BarTenderApp({super.key, required this.initialUrl});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'BarTender',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: Colors.amber),
        useMaterial3: true,
      ),
      home: initialUrl.isNotEmpty
          ? HomeScreen(serverUrl: initialUrl)
          : const SetupScreen(),
    );
  }
}
