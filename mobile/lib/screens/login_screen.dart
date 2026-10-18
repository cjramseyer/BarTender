import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import '../models/team_member.dart';
import '../services/api_service.dart';
import 'home_screen.dart';
import 'setup_screen.dart';

class LoginScreen extends StatefulWidget {
  final String serverUrl;

  const LoginScreen({super.key, required this.serverUrl});

  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _formKey = GlobalKey<FormState>();
  final _pinController = TextEditingController();
  final _secureStorage = const FlutterSecureStorage();
  late final ApiService _api;
  bool _loading = false;
  bool _loadingMembers = true;
  List<TeamMember> _teamMembers = [];
  String? _selectedUserId;
  String? _error;

  @override
  void initState() {
    super.initState();
    _api = ApiService(widget.serverUrl);
    _loadTeamMembers();
  }

  Future<void> _loadTeamMembers() async {
    setState(() {
      _loadingMembers = true;
      _error = null;
    });
    try {
      final users = await _api.fetchMobileUsers();
      if (!mounted) return;
      setState(() {
        _teamMembers = users;
        _selectedUserId = null;
      });
    } catch (error) {
      if (mounted) setState(() => _error = error.toString());
    } finally {
      if (mounted) setState(() => _loadingMembers = false);
    }
  }

  Future<void> _login() async {
    if (!_formKey.currentState!.validate()) return;
    final userId = _selectedUserId;
    if (userId == null) return;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final token = await _api.login(
        userId: userId,
        pin: _pinController.text,
      );
      await _secureStorage.write(key: 'mobile_token', value: token);
      if (!mounted) return;
      Navigator.of(context).pushReplacement(
        MaterialPageRoute(
          builder: (_) => HomeScreen(serverUrl: widget.serverUrl, token: token),
        ),
      );
    } catch (error) {
      if (mounted) setState(() => _error = error.toString());
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  void dispose() {
    _pinController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        leading: IconButton(
          tooltip: 'Change server',
          icon: const Icon(Icons.arrow_back),
          onPressed: () => Navigator.of(context).pushReplacement(
            MaterialPageRoute(
              builder: (_) => SetupScreen(initialUrl: widget.serverUrl),
            ),
          ),
        ),
        title: const Text('Sign in'),
      ),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(32),
          child: Form(
            key: _formKey,
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const Icon(Icons.lock_outline, size: 64, color: Colors.amber),
                const SizedBox(height: 20),
                Text('Sign in to BarTender',
                    textAlign: TextAlign.center,
                    style: Theme.of(context).textTheme.headlineSmall),
                const SizedBox(height: 8),
                Text(widget.serverUrl,
                    textAlign: TextAlign.center,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis),
                if (_error != null) ...[
                  const SizedBox(height: 16),
                  Text(_error!,
                      textAlign: TextAlign.center,
                      style: TextStyle(
                          color: Theme.of(context).colorScheme.error)),
                  const SizedBox(height: 8),
                  TextButton(
                    onPressed: _loadingMembers ? null : _loadTeamMembers,
                    child: const Text('Retry loading team members'),
                  ),
                ],
                const SizedBox(height: 24),
                DropdownButtonFormField<String>(
                  initialValue: _selectedUserId,
                  decoration: const InputDecoration(
                    labelText: 'Team member',
                    border: OutlineInputBorder(),
                  ),
                  hint: Text(_loadingMembers
                      ? 'Loading team members...'
                      : 'Select a team member'),
                  items: _teamMembers
                      .map(
                        (member) => DropdownMenuItem<String>(
                          value: member.id,
                          child: Text(member.name),
                        ),
                      )
                      .toList(),
                  onChanged: _loadingMembers || _teamMembers.isEmpty
                      ? null
                      : (value) => setState(() => _selectedUserId = value),
                  validator: (value) =>
                      value == null ? 'Team member is required' : null,
                ),
                const SizedBox(height: 12),
                TextFormField(
                  controller: _pinController,
                  obscureText: true,
                  decoration: const InputDecoration(
                      labelText: 'PIN', border: OutlineInputBorder()),
                  validator: (value) =>
                      value == null || value.isEmpty ? 'PIN is required' : null,
                ),
                const SizedBox(height: 20),
                FilledButton(
                  onPressed: _loading || _loadingMembers || _teamMembers.isEmpty
                      ? null
                      : _login,
                  child: _loading
                      ? const CircularProgressIndicator()
                      : const Text('Sign in'),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
